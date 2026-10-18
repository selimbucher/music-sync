"""Orchestrator: discover collections, plan, apply, confirm.

Order per collection is fixed by the invariants in merge.py:
read both sides -> reconcile identities -> plan -> guard -> apply -> commit
last_known from what was confirmed. A dry run stops before apply.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import asdict, dataclass, field

from . import match, merge
from .match import _duration_ok
from .config import Config
from .merge import Abort, Guard, Mode, Plan
from .models import ALBUM, APPLE, SPOTIFY, TRACK, Item, other_side
from .providers.base import AuthError, Listing, PlaylistRef, Provider, SearchQuota, Throttled
from .state import State

log = logging.getLogger(__name__)

LIKED = "liked"
ALBUMS = "albums"
FAVS = "favorites"

# A cached Spotify listing is trusted while its signature is unchanged and it
# is younger than this; after that a full listing is taken regardless.
CACHE_MAX_AGE_S = 6 * 3600


def stable_id(item: Item) -> str:
    """The id worth remembering across runs. Apple library/playlist rows have
    per-context ids; the catalog id is the durable one."""
    if item.side == APPLE:
        return item.catalog_id or item.native_id
    return item.native_id


@dataclass
class Outcome:
    collection: str
    label: str
    plan: Plan | None = None
    applied: dict[str, set[str]] = field(default_factory=lambda: {APPLE: set(), SPOTIFY: set()})
    quarantined: list[str] = field(default_factory=list)
    # adds that need a search while the search quota is exhausted; retried next run
    deferred: list[str] = field(default_factory=list)
    skipped: str | None = None
    # side -> descriptions of items planned for removal (dry runs show them)
    removals: dict[str, list[str]] = field(default_factory=lambda: {APPLE: [], SPOTIFY: []})

    def line(self) -> str:
        if self.skipped:
            return f"{self.label}: skipped — {self.skipped}"
        if self.plan is None:
            return f"{self.label}: aborted before planning"
        c = self.plan.counts()
        q = f", {len(self.quarantined)} unmatched" if self.quarantined else ""
        b = f", {len(self.plan.blocked)} in backoff" if self.plan.blocked else ""
        b += f", {len(self.deferred)} deferred (search quota)" if self.deferred else ""
        return (
            f"{self.label}: apple +{len(self.applied[APPLE])}/{c['add_apple']} -{c['del_apple']}, "
            f"spotify +{len(self.applied[SPOTIFY])}/{c['add_spotify']} -{c['del_spotify']}{q}{b}"
        )


class Engine:
    def __init__(self, apple: Provider, spotify: Provider, state: State, cfg: Config, dry_run: bool = False):
        self.apple, self.spotify, self.state, self.cfg = apple, spotify, state, cfg
        self.dry = dry_run
        self.run = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.guard = Guard(cfg.max_delete_ratio, cfg.max_delete_count, max_add_count=cfg.max_add_count)
        self.outcomes: list[Outcome] = []
        self.needs_review: list[str] = []
        self._code_cache: dict[tuple[str, str], list[Item]] = {}
        self._snapshots: dict[str, str | None] = {}   # spotify playlist id -> snapshot
        self._created: set[str] = set()               # pairs whose playlist was created this run
        self._sp_pls: dict[str, PlaylistRef] | None = None
        self._favs_pid: str | None = None             # the Spotify playlist mirroring Apple ★
        self._sleep = time.sleep                      # stubbed in tests

    def side(self, name: str) -> Provider:
        return self.apple if name == APPLE else self.spotify

    def log(self, action: str, **kw) -> None:
        if not self.dry:
            self.state.log(self.run, action, **kw)

    # -- entry points ---------------------------------------------------------

    def seed(self, master: str = APPLE, prune_playlists: bool = True, union: bool = False) -> list[Outcome]:
        """First run.

        union: every collection becomes the union of both sides; playlists
        missing on one side are created there; nothing is removed anywhere.
        The choice is remembered, so collections that appear later (or that a
        throttled seed did not reach) are unioned by ``sync`` on their own.

        Otherwise mirror := master exactly, master untouched: playlists that
        exist only on the mirror are deleted unless ``prune_playlists`` is off,
        and track order is copied where the mirror can reorder."""
        self.apple.probe()
        self.spotify.probe()
        if union and not self.dry:
            self.state.set_meta("seed_mode", "union")
        self._setup_favorites()
        self._pair_playlists(seed_master=None if union else master, prune=False if union else prune_playlists)
        for key, label, kind in self._collections():
            if self.state.is_seeded(key) and key not in self._created:
                self.outcomes.append(Outcome(key, label, skipped="already seeded"))
                continue
            if union:
                self._sync_one(key, label, kind, Mode.UNION, None)
            else:
                self._sync_one(key, label, kind, Mode.SEED, master)
        return self.outcomes

    def sync(self) -> list[Outcome]:
        if not self.state.any_seeded():
            # Before the first seed, even playlist pairing would write to Apple.
            self.outcomes.append(Outcome("-", "library", skipped="nothing seeded yet; run `music-sync seed`"))
            self.log("skip", detail="not seeded")
            return self.outcomes
        self.apple.probe()
        self.spotify.probe()
        self._setup_favorites()
        self._pair_playlists(seed_master=None, prune=False)
        union = self.state.get_meta("seed_mode") == "union"
        for key, label, kind in self._collections():
            if not self.state.is_seeded(key):
                if union:
                    self._sync_one(key, label, kind, Mode.UNION, None)
                else:
                    self.outcomes.append(Outcome(key, label, skipped="not seeded; run `music-sync seed`"))
                continue
            self._sync_one(key, label, kind, Mode.MERGE, None)
        return self.outcomes

    # -- collections ----------------------------------------------------------

    def _collections(self) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        # ★ first: starring a song on Apple can add it to the library, and
        # Liked (the library) should then see it in the same run.
        if self._favs_pid:
            out.append((FAVS, "Favourites (\u2605)", TRACK))
        if self.cfg.sync_liked:
            self.state.save_pair(LIKED, TRACK, "Liked songs")
            out.append((LIKED, "Liked songs", TRACK))
        if self.cfg.sync_albums:
            self.state.save_pair(ALBUMS, ALBUM, "Saved albums")
            out.append((ALBUMS, "Saved albums", ALBUM))
        if self.cfg.sync_playlists:
            for row in self.state.pairs():
                if row["collection"].startswith("pl:") and row["apple_id"] and row["spotify_id"]:
                    out.append((row["collection"], row["label"], TRACK))
        return out

    def _pair_row(self, key: str):
        return self.state.db.execute("SELECT * FROM pair WHERE collection=?", (key,)).fetchone()

    def _signature(self, key: str, side: str) -> str | None:
        """Cheap change signal for a Spotify collection, or None."""
        if side != SPOTIFY:
            return None
        if key == LIKED:
            return self.spotify.liked_signature()
        if key == ALBUMS:
            return None
        if key == FAVS:
            return self._snapshots.get(self._favs_pid)
        return self._snapshots.get(self._pair_row(key)["spotify_id"])

    def _listing(self, key: str, side: str, fresh: bool = False) -> Listing:
        prov = self.side(side)
        sig = None if fresh else self._signature(key, side)
        if sig:
            cached = self.state.cached_listing(key, side, sig, CACHE_MAX_AGE_S)
            if cached is not None:
                return Listing(items=[Item(**d) for d in cached], complete=True)
        if key == LIKED:
            listing = prov.liked()
        elif key == ALBUMS:
            listing = prov.albums()
        elif key == FAVS:
            listing = self.apple.favorites() if side == APPLE else self.spotify.playlist_items(self._favs_pid)
        else:
            row = self._pair_row(key)
            listing = prov.playlist_items(row["apple_id"] if side == APPLE else row["spotify_id"])
        if sig and listing.complete:
            self.state.store_listing(key, side, sig, [asdict(i) for i in listing.items])
        elif side == SPOTIFY and not fresh:
            self.state.drop_listing(key, side)
        return listing

    # -- playlist pairing -----------------------------------------------------

    def _spotify_playlists(self) -> dict[str, PlaylistRef]:
        """The account's own Spotify playlists, listed once per run."""
        if self._sp_pls is None:
            self._sp_pls = {p.native_id: p for p in self.spotify.playlists() if p.editable}
            self._snapshots = {pid: p.snapshot for pid, p in self._sp_pls.items()}
        return self._sp_pls

    def _setup_favorites(self) -> None:
        """Find (or create) the Spotify playlist that mirrors Apple's ★ songs."""
        name = self.cfg.spotify_favorites_playlist
        if not name:
            return
        pls = self._spotify_playlists()
        row = self._pair_row(FAVS)
        pid = row["spotify_id"] if row else None
        if pid and pid not in pls:
            # Deleting the playlist is not un-starring everything: start over
            # with a fresh copy instead of mirroring the deletion onto Apple.
            self.state.forget_collection(FAVS)
            self.needs_review.append(f"the \u2605 playlist '{row['label']}' is gone from Spotify; recreating it")
            pid = None
        if not pid:
            twin = next((p for p in pls.values() if p.name.strip().lower() == name.strip().lower()), None)
            if twin:
                pid = twin.native_id
            elif self.dry:
                n = len(self.apple.favorites().items)
                self.needs_review.append(f"[dry] would create Spotify playlist '{name}' with your {n} \u2605 songs")
                return
            else:
                pid = self.spotify.create_playlist(name, "Apple Music favourites, kept in sync")
                self._created.add(FAVS)
                self.log("create_playlist", collection=FAVS, side=SPOTIFY, label=name)
            self.state.save_pair(FAVS, TRACK, name, None, pid)
        self._favs_pid = pid

    def _pair_playlists(self, seed_master: str | None, prune: bool) -> None:
        """Reconcile the *set* of playlists before any contents are touched."""
        if not self.cfg.sync_playlists:
            return
        apple = {p.native_id: p for p in self.apple.playlists() if p.editable}
        # The ★ playlist is its own collection, never a regular pair.
        spotify = {pid: p for pid, p in self._spotify_playlists().items() if pid != self._favs_pid}
        known = [r for r in self.state.pairs() if r["collection"].startswith("pl:")]

        # 1. Existing pairs: deletion on one side, renames.
        deletions = 0
        for row in known:
            key, a, s = row["collection"], row["apple_id"], row["spotify_id"]
            a_here, s_here = a in apple, s in spotify
            if a_here and s_here:
                self._maybe_rename(key, row, apple[a], spotify[s])
                continue
            if not a_here and not s_here:
                self.state.forget_collection(key)
                continue
            if seed_master:
                master_here = a_here if seed_master == APPLE else s_here
                if master_here:
                    # Mirror copy vanished: forget the pair; step 2 recreates it.
                    self.state.forget_collection(key)
                elif prune:
                    mirror, mirror_id = other_side(seed_master), (s if seed_master == APPLE else a)
                    self._delete_playlist(key, row, mirror, mirror_id)
                    (spotify if mirror == SPOTIFY else apple).pop(mirror_id, None)
                else:
                    self.needs_review.append(
                        f"playlist '{row['label']}' exists only on the mirror (seed without --prune)")
                continue
            if deletions >= self.cfg.max_playlist_deletes:
                self.needs_review.append(
                    f"playlist '{row['label']}' missing on one side; over the per-run cap of "
                    f"{self.cfg.max_playlist_deletes}")
                continue
            other, other_id = (SPOTIFY, s) if not a_here else (APPLE, a)
            self._delete_playlist(key, row, other, other_id)
            (spotify if other == SPOTIFY else apple).pop(other_id, None)
            deletions += 1

        # 2. Unpaired: pair by name, else create on the far side.
        paired_a = {r["apple_id"] for r in self.state.pairs()}
        paired_s = {r["spotify_id"] for r in self.state.pairs()}
        by_name_s = {p.name.strip().lower(): p for p in spotify.values() if p.native_id not in paired_s}
        for side_name, pls in (("Apple", apple), ("Spotify", spotify)):
            names = [p.name.strip().lower() for p in pls.values()]
            for dup in sorted({n for n in names if names.count(n) > 1}):
                self.needs_review.append(
                    f"{side_name} has {names.count(dup)} playlists named '{dup}'; only the first is paired by name")
        for a_id, ap in apple.items():
            if a_id in paired_a:
                continue
            key = f"pl:{uuid.uuid4().hex[:12]}"
            twin = by_name_s.pop(ap.name.strip().lower(), None)
            if twin:
                self.state.save_pair(key, TRACK, ap.name, a_id, twin.native_id)
                self.log("pair", collection=key, label=ap.name,
                         detail={"apple": a_id, "spotify": twin.native_id})
                continue
            if self.dry:
                n = len(self.apple.playlist_items(a_id).items)
                self.needs_review.append(f"[dry] would create Spotify playlist '{ap.name}' with {n} tracks")
                continue
            s_id = self.spotify.create_playlist(ap.name, ap.description)
            self.state.save_pair(key, TRACK, ap.name, a_id, s_id)
            self._created.add(key)
            if seed_master is None:
                # Born after seeding: the new side is empty by construction, so
                # an empty last_known makes the first merge a plain copy.
                self.state.set_last_known(key, set())
                self.state.mark_seeded(key)
            self.log("create_playlist", collection=key, side=SPOTIFY, label=ap.name)

        for sp in by_name_s.values():
            if seed_master == APPLE:
                if not prune:
                    self.needs_review.append(
                        f"Spotify-only playlist '{sp.name}' left alone (seed without --prune)")
                elif self.dry:
                    log.info("[dry] would delete Spotify-only playlist '%s'", sp.name)
                else:
                    self.spotify.delete_playlist(sp.native_id)
                    self.log("delete_playlist", side=SPOTIFY, label=sp.name)
                continue
            key = f"pl:{uuid.uuid4().hex[:12]}"
            if self.dry:
                n = len(self.spotify.playlist_items(sp.native_id).items)
                self.needs_review.append(f"[dry] would create Apple playlist '{sp.name}' with {n} tracks")
                continue
            a_id = self.apple.create_playlist(sp.name, sp.description)
            self.state.save_pair(key, TRACK, sp.name, a_id, sp.native_id)
            self._created.add(key)
            self.state.set_last_known(key, set())
            self.state.mark_seeded(key)
            self.log("create_playlist", collection=key, side=APPLE, label=sp.name)

    def _maybe_rename(self, key: str, row, ap: PlaylistRef, sp: PlaylistRef) -> None:
        label = row["label"]
        a_changed, s_changed = ap.name != label, sp.name != label
        if not a_changed and not s_changed:
            return
        new = ap.name if a_changed else sp.name  # both changed: Apple wins
        target, target_id = (self.spotify, sp.native_id) if a_changed else (self.apple, ap.native_id)
        if (ap.name if a_changed else sp.name) == (sp.name if a_changed else ap.name):
            self.state.save_pair(key, TRACK, new, ap.native_id, sp.native_id)
            return
        if self.dry:
            log.info("[dry] would rename '%s' -> '%s'", label, new)
            return
        target.rename_playlist(target_id, new)
        self.state.save_pair(key, TRACK, new, ap.native_id, sp.native_id)
        self.log("rename_playlist", collection=key, label=f"{label} -> {new}")

    def _delete_playlist(self, key: str, row, side: str, native_id: str) -> None:
        if self.dry:
            log.info("[dry] would delete %s playlist '%s'", side, row["label"])
            return
        self.side(side).delete_playlist(native_id)
        self.state.forget_collection(key)
        self.log("delete_playlist", collection=key, side=side, label=row["label"])

    # -- identity reconciliation ---------------------------------------------

    def _lookup(self, kind: str, code: str) -> list[Item]:
        k = (kind, code)
        if k not in self._code_cache:
            self._code_cache[k] = self.spotify.lookup_codes(kind, [code]).get(code, [])
        return self._code_cache[k]

    def _reconcile(self, key: str, kind: str, a_items: list[Item], s_items: list[Item],
                   seed: bool = False) -> dict[str, str]:
        """Spotify native id -> identity, for every Spotify item we can pin.

        Known mappings come from state. Unknown ones are tried against Apple
        items with the same artist+title within duration tolerance: a unique
        candidate is accepted outright; several candidates are settled by
        round-tripping each Apple code through Spotify's code search to this
        very Spotify id. Searches count against Spotify's daily quota, so they
        are spent only on the ambiguous cases. A wrong acceptance here only
        keeps Spotify's edition of a song; a wrong rejection removes and
        re-adds it. Without this step a first seed would read every unmapped
        Spotify track as "absent from Apple" and remove it.
        """
        ids = self.state.identities_for(SPOTIFY, [it.native_id for it in s_items])
        by_fuzzy: dict[str, list[Item]] = {}
        for it in a_items:
            # Items without a code (no catalog link) are keyed on their stable
            # library id; leaving them out would make their Spotify twin look
            # absent from Apple and get it removed.
            by_fuzzy.setdefault(it.fuzzy_key, []).append(it)
        for it in s_items:
            if it.native_id in ids:
                continue
            cands = [c for c in by_fuzzy.get(it.fuzzy_key, []) if _duration_ok(it, c)]
            if len(cands) == 1:
                chosen = cands[0]
            else:
                try:
                    chosen = next((c for c in cands if c.code
                                   and any(b.native_id == it.native_id for b in self._lookup(kind, c.code))), None)
                except SearchQuota as e:
                    if seed:
                        # Guessing here could remove the wrong edition; wait for the quota.
                        raise Abort(f"ambiguous match for {it.describe()!r} needs a search: {e}") from e
                    chosen = None  # stays provisional: an add to defer, never a removal
            if chosen is None:
                continue
            identity = match.identity_of(chosen)
            ids[it.native_id] = identity
            if not self.dry:
                self.state.remember_identity(identity, kind, SPOTIFY, it.native_id, it.describe())
                self.state.remember_identity(identity, kind, APPLE, stable_id(chosen), it.describe())
        return ids

    # -- one collection -------------------------------------------------------

    def _sync_one(self, key: str, label: str, kind: str, mode: Mode, master: str | None) -> None:
        out = Outcome(key, label)
        self.outcomes.append(out)
        try:
            la = self._listing(key, APPLE)
            ls = self._listing(key, SPOTIFY)
            a_items = self.apple.enrich(la.items)
            s_items = ls.items

            a_by = {match.identity_of(it): it for it in a_items}
            first_run = mode in (Mode.SEED, Mode.UNION)
            s_ids = self._reconcile(key, kind, a_items, s_items, seed=first_run)
            s_by = {s_ids.get(it.native_id) or match.identity_of(it): it for it in s_items}

            if mode == Mode.SEED:
                assert master
                mirror = other_side(master)
                m_list, x_list = (la, ls) if master == APPLE else (ls, la)
                if not m_list.complete:
                    raise Abort(f"{label}: master listing incomplete ({m_list.error})")
                plan = merge.plan_seed(key, label, master,
                                       set(a_by if master == APPLE else s_by),
                                       set(s_by if master == APPLE else a_by))
                if not x_list.complete:
                    plan.remove[mirror] = set()
                    self.needs_review.append(f"{label}: mirror listing incomplete; seeded adds only")
            else:
                merge.require_complete({APPLE: la.complete, SPOTIFY: ls.complete}, label)
                history = set() if mode == Mode.UNION else self.state.last_known(key)
                plan = merge.plan_merge(key, label, set(a_by), set(s_by), history, mode=mode)

            self.guard.check(plan, {APPLE: len(a_by), SPOTIFY: len(s_by)})
            merge.suppress_quarantined(plan, self.state, key)
            out.plan = plan
            for side, own in ((APPLE, a_by), (SPOTIFY, s_by)):
                out.removals[side] = sorted(own[i].describe() for i in plan.remove[side] if i in own)
            aliases: dict[str, str] = {}
            if not self.dry:
                if not plan.empty:
                    for side in (APPLE, SPOTIFY):
                        self._apply_adds(key, kind, side, plan, a_by, s_by, out, aliases)
                    for side in (APPLE, SPOTIFY):
                        self._apply_removes(key, kind, side, plan, a_by, s_by, out)
                self._commit(key, plan, out.applied, aliases, seed=first_run)
            if mode == Mode.SEED and key.startswith("pl:"):
                # Order is part of "exactly"; a dry run only reports it.
                self._match_order(key, label, master, a_items, s_items, s_ids, aliases, out)
        except Abort as e:
            out.skipped = str(e)
            self.needs_review.append(str(e))
            self.log("abort", collection=key, label=label, detail=str(e))

    def _match_order(self, key, label, master, a_items, s_items, s_ids, aliases, out: Outcome) -> None:
        """Seed only: put the mirror playlist in the master's order."""
        mirror = other_side(master)
        m_items, x_items = (a_items, s_items) if master == APPLE else (s_items, a_items)
        target: list[Item] = []
        for it in m_items:
            identity = match.identity_of(it) if master == APPLE else s_ids.get(it.native_id, match.identity_of(it))
            identity = aliases.get(identity, identity)
            nid = self.state.native_id(identity, mirror)
            if nid:
                target.append(Item(kind=it.kind, side=mirror, native_id=nid, title=it.title, artist=it.artist))
        current = [stable_id(it) for it in x_items]
        wanted = [it.native_id for it in target]
        if not target or current == wanted or [c for c in current if c in wanted] == wanted:
            return
        if self.dry:
            log.info("[dry] would reorder %s on %s", label, mirror)
            return
        try:
            self.side(mirror).replace_playlist_items(self._pair_row(key)[f"{mirror}_id"], target)
            self.state.drop_listing(key, mirror)
            self.log("reorder", collection=key, side=mirror, label=label, detail=len(target))
        except NotImplementedError as e:
            self.needs_review.append(f"{label}: {e}")
        except Exception as e:  # noqa: BLE001
            self.needs_review.append(f"{label}: reorder on {mirror} failed: {e}")

    def _apply_adds(self, key, kind, side, plan: Plan, a_by, s_by, out: Outcome, aliases) -> None:
        if not plan.add[side]:
            return
        far = other_side(side)
        far_by = a_by if far == APPLE else s_by
        own_by = s_by if far == APPLE else a_by
        prov = self.side(side)
        present = {stable_id(it) for it in own_by.values()}
        resolved: list[tuple[str, Item]] = []
        # Source order, so a copied playlist reads like the original.
        order = sorted(plan.add[side], key=lambda i: (far_by[i].position is None, far_by[i].position or 0, i))
        for identity in order:
            origin = far_by[identity]
            known = self.state.native_id(identity, side)
            if known:
                it = Item(kind=kind, side=side, native_id=known, title=origin.title, artist=origin.artist,
                          duration_ms=origin.duration_ms)
                resolved.append((identity, it.with_catalog(known) if side == APPLE else it))
                continue
            try:
                m = match.resolve(origin, self.apple, self.spotify)
            except SearchQuota:
                out.deferred.append(origin.describe())
                continue
            if not m:
                reason = "no catalog id" if (origin.side == APPLE and not origin.catalog_id) else "no confident match"
                self.state.quarantine(key, identity, side, origin.describe(), reason)
                out.quarantined.append(origin.describe())
                self.log("quarantine", collection=key, side=side, label=origin.describe(), detail=reason)
                continue
            self.state.remember_identity(m.identity, kind, side, stable_id(m.far), origin.describe())
            self.state.remember_identity(m.identity, kind, far, stable_id(origin), origin.describe())
            if m.identity != identity:
                aliases[identity] = m.identity
            resolved.append((identity, m.far))

        # A planned removal that is the very item being added is a no-op: the
        # item was here all along under an identity we only just pinned.
        if resolved and plan.remove[side]:
            adding = {stable_id(it) for _, it in resolved}
            for rid in list(plan.remove[side]):
                if stable_id(own_by[rid]) in adding:
                    plan.remove[side].discard(rid)
                    resolved = [(i, it) for i, it in resolved if stable_id(it) != stable_id(own_by[rid])]
                    plan.stable.add(aliases.get(rid, rid))
        # Already here under an identity reconciliation could not pin: writing
        # it again would duplicate a playlist entry. Count it as landed.
        fresh: list[tuple[str, Item]] = []
        for identity, it in resolved:
            if stable_id(it) in present:
                out.applied[side].add(identity)
                self.log("already_present", collection=key, side=side, label=it.describe())
            else:
                fresh.append((identity, it))
                present.add(stable_id(it))
        if not fresh:
            return
        try:
            self._write(prov, key, "add", [it for _, it in fresh])
        except (AuthError, Throttled):
            raise
        except Exception as e:  # noqa: BLE001
            # One bad item fails the whole batch; find it, add the rest. The
            # culprit goes to quarantine so it is retried with backoff instead
            # of failing (and mailing) every run.
            ok = []
            for identity, it in (fresh if len(fresh) > 1 else []):
                try:
                    self._write(prov, key, "add", [it])
                    ok.append((identity, it))
                except (AuthError, Throttled):
                    raise
                except Exception as e1:  # noqa: BLE001
                    self._add_failed(key, side, plan, aliases.get(identity, identity), it, e1, out)
            if len(fresh) == 1:
                self._add_failed(key, side, plan, aliases.get(fresh[0][0], fresh[0][0]), fresh[0][1], e, out)
            fresh = ok
            if not fresh:
                return
        self.state.drop_listing(key, side)
        landed = self._verify_adds(key, side, fresh, {stable_id(it) for it in own_by.values()})
        for identity, it in fresh:
            if identity in landed:
                out.applied[side].add(identity)
                self.state.clear_quarantine(key, aliases.get(identity, identity), side)
                self.log("add", collection=key, side=side, label=it.describe())
            else:
                # Under the identity the next run will plan with (the matched
                # one), or the backoff would never find this row.
                self.state.quarantine(key, aliases.get(identity, identity), side, it.describe(),
                                      "write accepted but never appeared")
                out.quarantined.append(it.describe())
                self.log("add_unconfirmed", collection=key, side=side, label=it.describe())
        missing = len(fresh) - len([i for i, _ in fresh if i in landed])
        if missing:
            self.needs_review.append(f"{plan.label}: {missing} add(s) on {side} were accepted but never appeared")

    def _add_failed(self, key, side, plan: Plan, identity: str, it: Item, err: Exception, out: Outcome) -> None:
        self.state.quarantine(key, identity, side, it.describe(), f"write failed: {str(err)[:200]}")
        out.quarantined.append(it.describe())
        self.needs_review.append(f"{plan.label}: add of {it.describe()!r} on {side} failed: {err}")
        self.log("add_failed", collection=key, side=side, label=it.describe(), detail=str(err))

    # Apple shows additions with a lag; total wait is bounded at ~37 s.
    VERIFY_DELAYS = (0, 2, 5, 10, 20)

    def _verify_adds(self, key: str, side: str, written: list[tuple[str, Item]], before: set[str]) -> set[str]:
        """Identities whose add shows up in a fresh listing of ``side``.

        Apple answers 202 and silently drops ids it cannot add. Trusting the
        status code would put a missing item into last_known, and the next run
        would mirror its absence as a deletion on the other side."""
        pending = dict(written)
        landed: set[str] = set()
        for delay in self.VERIFY_DELAYS:
            if delay:
                self._sleep(delay)
            listing = self._listing(key, side, fresh=True)
            if not listing.complete:
                continue
            items = self.apple.enrich(listing.items) if side == APPLE else listing.items
            ids = {stable_id(i) for i in items}
            codes = {i.code for i in items if i.code}
            new_keys = {i.fuzzy_key for i in items if stable_id(i) not in before}
            for identity, it in list(pending.items()):
                if stable_id(it) in ids or (it.code and it.code in codes) or it.fuzzy_key in new_keys:
                    landed.add(identity)
                    del pending[identity]
            if not pending:
                break
        return landed

    def _apply_removes(self, key, kind, side, plan: Plan, a_by, s_by, out: Outcome) -> None:
        if not plan.remove[side]:
            return
        own = a_by if side == APPLE else s_by
        pairs = [(i, own[i]) for i in sorted(plan.remove[side]) if i in own]
        items = [it for _, it in pairs]
        try:
            self._write(self.side(side), key, "remove", items)
        except (AuthError, Throttled):
            raise
        except Exception as e:  # noqa: BLE001
            # Keep the item in last_known so it is retried as a removal next
            # run, not resurrected as an add on the other side.
            plan.stable |= plan.remove[side]
            plan.remove[side] = set()
            self.needs_review.append(f"{plan.label}: remove on {side} failed: {e}")
            self.log("remove_failed", collection=key, side=side, detail=str(e))
            return
        self.state.drop_listing(key, side)
        still = self._verify_removes(key, side, pairs)
        for identity, it in pairs:
            if identity in still:
                # Keep it on the books: retried as a removal next run rather
                # than read as a fresh add and copied back to the other side.
                plan.remove[side].discard(identity)
                plan.stable.add(identity)
                self.log("remove_unconfirmed", collection=key, side=side, label=it.describe())
            else:
                self.log("remove", collection=key, side=side, label=it.describe())
        if still:
            self.needs_review.append(
                f"{plan.label}: {len(still)} removal(s) on {side} were accepted but the items are still there")

    def _verify_removes(self, key: str, side: str, removed: list[tuple[str, Item]]) -> set[str]:
        """Identities that a fresh listing still shows after their removal."""
        pending = dict(removed)
        for delay in self.VERIFY_DELAYS:
            if delay:
                self._sleep(delay)
            listing = self._listing(key, side, fresh=True)
            if not listing.complete:
                continue
            items = self.apple.enrich(listing.items) if side == APPLE else listing.items
            ids = {stable_id(i) for i in items}
            codes = {i.code for i in items if i.code}
            pending = {i: it for i, it in pending.items()
                       if stable_id(it) in ids or (it.code and it.code in codes)}
            if not pending:
                break
        return set(pending)

    def _write(self, prov: Provider, key: str, op: str, items: list[Item]) -> None:
        if key == FAVS:
            if prov.side == APPLE:
                (prov.favorite if op == "add" else prov.unfavorite)(items)
            else:
                (prov.add_to_playlist if op == "add" else prov.remove_from_playlist)(self._favs_pid, items)
        elif key == LIKED:
            (prov.like if op == "add" else prov.unlike)(items)
        elif key == ALBUMS:
            (prov.save_albums if op == "add" else prov.unsave_albums)(items)
        else:
            row = self._pair_row(key)
            pid = row["apple_id"] if prov.side == APPLE else row["spotify_id"]
            (prov.add_to_playlist if op == "add" else prov.remove_from_playlist)(pid, items)

    def _commit(self, key: str, plan: Plan, applied, aliases: dict[str, str], seed: bool) -> None:
        survived = merge.confirmed_last_known(plan, applied)
        survived = {aliases.get(i, i) for i in survived}
        with self.state.transaction():
            self.state.set_last_known(key, survived)
            if seed:
                self.state.mark_seeded(key)
        self.log("commit", collection=key, detail={"last_known": len(survived), **plan.counts()})

    # -- reporting -----------------------------------------------------------------

    def report(self) -> str:
        lines = [f"run {self.run}{' (dry run)' if self.dry else ''}"]
        for o in self.outcomes:
            lines.append("  " + o.line())
            if self.dry:
                for side in (APPLE, SPOTIFY):
                    for name in o.removals[side]:
                        lines.append(f"      - {side}: {name}")
        if self.needs_review:
            lines.append("needs review:")
            lines += ["  - " + r for r in self.needs_review]
        return "\n".join(lines)
