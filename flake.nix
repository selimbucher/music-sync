{
  description = "Bidirectional Apple Music <-> Spotify sync";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in {
      packages = forAll (pkgs: rec {
        music-sync = pkgs.python3Packages.buildPythonApplication {
          pname = "music-sync";
          version = "0.1.0";
          pyproject = true;
          src = ./.;
          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = [ pkgs.python3Packages.requests ];
          nativeCheckInputs = [ pkgs.python3Packages.pytestCheckHook ];
        };
        default = music-sync;
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [ (pkgs.python3.withPackages (p: [ p.requests p.pytest ])) ];
        };
      });

      nixosModules.default = { config, lib, pkgs, ... }:
        let
          cfg = config.services.music-sync;
          pkg = self.packages.${pkgs.stdenv.hostPlatform.system}.music-sync;
        in {
          options.services.music-sync = {
            enable = lib.mkEnableOption "Apple Music <-> Spotify sync";
            interval = lib.mkOption {
              type = lib.types.str;
              default = "15min";
              description = "systemd OnUnitActiveSec between runs. Neither API pushes changes, so this is a poll.";
            };
            appleUserTokenFile = lib.mkOption { type = lib.types.path; description = "media-user-token cookie from music.apple.com"; };
            spotifyClientIdFile = lib.mkOption { type = lib.types.path; };
            spotifyClientSecretFile = lib.mkOption { type = lib.types.path; };
            spotifyRefreshTokenFile = lib.mkOption {
              type = lib.types.path;
              description = "Written by `music-sync auth spotify`; the service must be able to rewrite it on rotation.";
            };
            appleLiked = lib.mkOption {
              type = lib.types.enum [ "library" "favorites" ];
              default = "library";
              description = "What Spotify's Liked Songs are on Apple Music: the library (+) or Favorites (the star).";
            };
            notifyEmail = lib.mkOption { type = lib.types.nullOr lib.types.str; default = null; };
            notifyFrom = lib.mkOption { type = lib.types.str; default = "music-sync@${config.networking.hostName}"; };
            after = lib.mkOption {
              type = lib.types.listOf lib.types.str;
              default = [ ];
              description = "Extra units to order after, e.g. the one that installs the secret files.";
            };
            extraEnvironment = lib.mkOption { type = lib.types.attrsOf lib.types.str; default = { }; };
          };

          config = lib.mkIf cfg.enable (let
            # Root-owned secret files reach the unprivileged service as systemd
            # credentials; config.py reads $CREDENTIALS_DIRECTORY/<name>.
            credentials = [
              "apple-user-token:${toString cfg.appleUserTokenFile}"
              "spotify-client-id:${toString cfg.spotifyClientIdFile}"
              "spotify-client-secret:${toString cfg.spotifyClientSecretFile}"
            ];
            env = {
              MUSIC_SYNC_STATE = "/var/lib/music-sync/state.db";
              # Rewritten on rotation, so it lives in the state dir, not in credentials.
              MUSIC_SYNC_SPOTIFY_REFRESH_TOKEN_FILE = toString cfg.spotifyRefreshTokenFile;
              MUSIC_SYNC_NOTIFY_FROM = cfg.notifyFrom;
              MUSIC_SYNC_APPLE_LIKED = cfg.appleLiked;
            } // lib.optionalAttrs (cfg.notifyEmail != null) { MUSIC_SYNC_NOTIFY_EMAIL = cfg.notifyEmail; }
              // cfg.extraEnvironment;
            hardening = {
              User = "music-sync";
              Group = "music-sync";
              StateDirectory = "music-sync";
              LoadCredential = credentials;
              PrivateTmp = true;
              ProtectSystem = "strict";
              ProtectHome = true;
              NoNewPrivileges = true;
            };
            # Manual runs (seed, dry-run, status) with the service's exact
            # credentials and environment: `sudo music-sync-admin seed --dry-run`.
            admin = pkgs.writeShellScriptBin "music-sync-admin" ''
              exec ${pkgs.systemd}/bin/systemd-run --wait --pipe --collect --quiet \
                --unit "music-sync-manual-$$" \
                ${lib.concatMapStringsSep " " (c: "-p LoadCredential=${c}") credentials} \
                ${lib.concatMapStringsSep " " (n: "-p ${n}") [
                  "User=music-sync" "Group=music-sync" "StateDirectory=music-sync"
                  "PrivateTmp=yes" "ProtectSystem=strict" "ProtectHome=yes" "NoNewPrivileges=yes"
                ]} \
                ${lib.concatMapStringsSep " " (n: "-E ${n}=${lib.escapeShellArg env.${n}}") (builtins.attrNames env)} \
                ${pkg}/bin/music-sync "$@"
            '';
          in {
            systemd.services.music-sync = {
              description = "Apple Music <-> Spotify sync";
              wants = [ "network-online.target" ];
              after = [ "network-online.target" ] ++ cfg.after;
              path = [ pkg ];
              environment = env;
              serviceConfig = {
                Type = "oneshot";
                ExecStart = "${pkg}/bin/music-sync sync";
                # A run that needs review (2), lost credentials (3) or was
                # throttled (4) is not a unit failure; the mail says why.
                SuccessExitStatus = "0 2 3 4";
              } // hardening;
            };

            systemd.timers.music-sync = {
              wantedBy = [ "timers.target" ];
              timerConfig = {
                OnBootSec = "5min";
                OnUnitActiveSec = cfg.interval;
                RandomizedDelaySec = "2min";
                Unit = "music-sync.service";
              };
            };

            users.users.music-sync = { isSystemUser = true; group = "music-sync"; };
            users.groups.music-sync = { };

            environment.systemPackages = [ pkg admin ];
          });
        };
    };
}
