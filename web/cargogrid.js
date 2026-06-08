"use strict";

// Shared cargo-grid renderer (dashboard Grid tab + all-ships /grids.html), drawn
// as an isometric 3D scene. Firefox-safe recipe: boxes positioned with
// translate3d (never left/top inside the 3D context) and faces grown from a
// single transform-origin:0 0 corner (no percentage origins).
//
// Three modes:
//   • volume  — each sub-grid is one extruded wireframe box (the empty geometry).
//               Used by the catalog page, which has no mission data.
//   • fill    — opts.segments given: each sub-grid is drawn as a flat floor with
//               1-SCU cubes stacked on it, colored by destination, so you can see
//               how much of the grid each mission's cargo takes up.
//   • packed  — opts.packed (from packCargo) given: a flat floor with discrete
//               sized cargo boxes placed by the 3D first-fit packer (test mode).
//
// A ship grid is the `groups` list from ships.json: bays, each
// `{x, z, grids:[{x,y,z,width,height,length, …}]}`. x = width, z = depth, y =
// vertical level; a sub-grid's SCU = width × length × height.
(function (global) {
  const esc = (s) => (s == null ? "" : String(s)).replace(/[&<>"]/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const num = (n) => (n == null ? "" : Number(n).toLocaleString());
  const cellScu = (c) => c.width * c.height * c.length;

  const A = 56 * Math.PI / 180, C = 45 * Math.PI / 180;
  const cosA = Math.cos(A), sinA = Math.sin(A), cosC = Math.cos(C), sinC = Math.sin(C);
  const STAGE_TF = "rotateX(56deg) rotateZ(45deg)";
  function project(x, y, z) {
    const sx = x * cosC - y * sinC;
    const y1 = x * sinC + y * cosC;
    return [sx, y1 * cosA - z * sinA];
  }

  function bayBox(cells) {
    const minX = Math.min(...cells.map(c => c.x));
    const minZ = Math.min(...cells.map(c => c.z));
    return {
      minX, minZ,
      w: Math.max(...cells.map(c => c.x + c.width)) - minX,
      l: Math.max(...cells.map(c => c.z + c.length)) - minZ,
    };
  }

  // 180° yaw about the bay centre, applied for rear-hatch ships so the hatch/loading
  // door faces the viewer (down-left) instead of the nose. Mirrors a normalized
  // (widthStart, depthStart) corner; box sizes are unchanged (it's a rotation, not a
  // reflection). `flip` off → identity. Every coord→screen path runs through this so
  // the boxes, overlays and scene bbox all stay consistent.
  function flipXZ(dx, dz, w, l, box, flip) {
    return flip ? [box.w - dx - w, box.l - dz - l] : [dx, dz];
  }

  // Scene element size + stage offset, from the screen bbox over every cell
  // corner (full volume), so both modes share one stable layout.
  function sceneMetrics(cells, box, S, flip) {
    let mnx = Infinity, mny = Infinity, mxx = -Infinity, mxy = -Infinity;
    for (const c of cells) {
      for (const xx of [c.x - box.minX, c.x - box.minX + c.width])
        for (const yy of [c.z - box.minZ, c.z - box.minZ + c.length])
          for (const z of [c.y || 0, (c.y || 0) + c.height]) {
            const x = flip ? box.w - xx : xx, y = flip ? box.l - yy : yy;
            const [sx, sy] = project(x * S, y * S, z * S);
            if (sx < mnx) mnx = sx; if (sx > mxx) mxx = sx;
            if (sy < mny) mny = sy; if (sy > mxy) mxy = sy;
          }
    }
    const pad = S + 26;   // room for the ▲ FWD marker + bay-name tags overhanging the cell bbox
    return {
      w: Math.ceil(mxx - mnx) + pad * 2, h: Math.ceil(mxy - mny) + pad * 2,
      left: Math.round(-mnx + pad), top: Math.round(-mny + pad),
    };
  }

  // The 5 faces of a 3D box (bottom omitted), as one transform recipe shared by every box
  // renderer below. It owns ONLY the Firefox-safe transforms + per-face dimensions; the
  // caller's `faceHtml(i, fw, fh, transform)` supplies the markup/class/colour. Face order
  // 0..4 = top, two depth walls, two side walls — see FACE_CLASS / the [top,yw,yw,xw,xw]
  // colour arrays callers index by `i`.
  function boxFaces(W, L, H, faceHtml) {
    return [
      faceHtml(0, W, L, `translateZ(${H}px)`),
      faceHtml(1, W, H, `rotateX(90deg)`),
      faceHtml(2, W, H, `translateY(${L}px) rotateX(90deg)`),
      faceHtml(3, H, L, `rotateY(-90deg)`),
      faceHtml(4, H, L, `translateX(${W}px) rotateY(-90deg)`),
    ].join("");
  }
  const FACE_CLASS = ["cg-top", "cg-yf", "cg-yf", "cg-xf", "cg-xf"];

  // 3D box (5 faces, bottom omitted) from corner (X,Y,base) px, size W×L×H px. One shared
  // `bg` style + the cg-top/cg-yf/cg-xf face classes — used by the wireframe volume mode.
  function faces(W, L, H, cls, bg, extra) {
    return boxFaces(W, L, H, (i, fw, fh, tf) =>
      `<div class="${cls} ${FACE_CLASS[i]}${extra}" style="width:${fw}px;height:${fh}px;transform-origin:0 0;transform:${tf};${bg}"></div>`);
  }

  // ---- volume mode (catalog): one wireframe box per sub-grid ----
  function volumeBoxHtml(c, box, S, flip) {
    const [dx, dz] = flipXZ(c.x - box.minX, c.z - box.minZ, c.width, c.length, box, flip);
    const X = dx * S, Y = dz * S, base = (c.y || 0) * S;
    const W = c.width * S, L = c.length * S, H = c.height * S;
    const bg = `background-size:${S}px ${S}px`;
    const horiz = c.preferHorizontal ? " cg-horiz" : "";
    const title = [
      `${c.width}×${c.length}×${c.height} = ${cellScu(c)} SCU`,
      c.maxSize ? `max box ${c.maxSize} SCU` : "",
      c.name || "",
    ].filter(Boolean).join(" · ");
    return `<div class="cg-box" title="${esc(title)}" style="transform:translate3d(${X}px,${Y}px,${base}px)">${faces(W, L, H, "cf", bg, horiz)}</div>`;
  }

  // ---- fill mode: floor per sub-grid + 1-SCU cubes ----
  function floorHtml(c, box, S, flip) {
    const [dx, dz] = flipXZ(c.x - box.minX, c.z - box.minZ, c.width, c.length, box, flip);
    const X = dx * S, Y = dz * S, base = (c.y || 0) * S;
    return `<div class="cg-floor3d" style="transform:translate3d(${X}px,${Y}px,${base}px);width:${c.width * S}px;height:${c.length * S}px;background-size:${S}px ${S}px"></div>`;
  }
  // one 1×1×1 cube at normalized cell (x width, y depth, z level), tinted by hue.
  function cubeHtml(x, y, z, hue, S, label) {
    const top = `hsl(${hue},68%,58%)`, yw = `hsl(${hue},64%,45%)`, xw = `hsl(${hue},58%,33%)`;
    const ed = "rgba(0,0,0,.45)";
    const sty = (bgc) => `width:${S}px;height:${S}px;border:1px solid ${ed};background:${bgc}`;
    const col = [top, yw, yw, xw, xw];
    const f = boxFaces(S, S, S, (i, fw, fh, tf) =>
      `<div class="cgc" style="${sty(col[i])};transform-origin:0 0;transform:${tf}"></div>`);
    return `<div class="cg-box"${label ? ` title="${esc(label)}"` : ""} style="transform:translate3d(${x * S}px,${y * S}px,${z * S}px)">${f}</div>`;
  }

  // Fill order for a bay: floor layer first across all sub-grids, then stack up.
  // Returns normalized {x, y, z} positions in load order.
  function fillOrder(cells, box) {
    const maxLvl = Math.max(...cells.map(c => (c.y || 0) + c.height));
    const out = [];
    for (let lvl = 0; lvl < maxLvl; lvl++) {
      for (const c of cells) {
        const base = c.y || 0;
        if (lvl < base || lvl >= base + c.height) continue;
        for (let row = 0; row < c.length; row++)
          for (let col = 0; col < c.width; col++)
            out.push({ x: (c.x - box.minX) + col, y: (c.z - box.minZ) + row, z: lvl });
      }
    }
    return out;
  }

  // ---- packed mode: discrete sized cargo boxes placed by a 3D first-fit packer ----
  // The packing invariants below (yaw-only orientations, no-float support, the per-tier
  // container cap, primary/secondary hold classification) are locked by
  // tests/cargogrid.test.js (run via `npm test`).
  // Standard SC container sizes as [width, length, height] in SCU units (height is
  // the vertical extent; the long axis lies flat — long boxes are never stood on
  // end). Each size gets a hue.
  const CARGO_SIZES = [
    { scu: 1,  dims: [1, 1, 1], hue: 200 },
    { scu: 2,  dims: [1, 2, 1], hue: 150 },
    { scu: 4,  dims: [2, 2, 1], hue: 95 },
    { scu: 8,  dims: [2, 2, 2], hue: 48 },
    { scu: 16, dims: [2, 4, 2], hue: 22 },
    { scu: 24, dims: [2, 6, 2], hue: 320 },
    { scu: 32, dims: [2, 8, 2], hue: 275 },
  ];

  // Allowed orientations: yaw only — swap the footprint (width↔length); the height
  // stays vertical, so a long box can't be tipped onto its end.
  function orientations([w, l, h]) {
    return w === l ? [[w, l, h]] : [[w, l, h], [l, w, h]];
  }

  // Pack an ORDERED list of boxes into the grid, IN THE GIVEN ORDER (first packed
  // = lowest, so callers control layering / load order). Each item is
  // {dims:[w,l,h], hue, scu, dest?}. Floor-first first-fit, no overlap, and boxes
  // above the floor must rest on a fully-occupied footprint (no float/overhang).
  // Returns placed boxes (normalized bay coords) + the items that didn't fit.
  // Floor-first first-fit packer. Each item may carry an optional band window
  // `win = {axis:'d'(depth)|'w'(width), lo, hi}` (bay-normalized coords) confining it
  // to a slice of the access axis — that's how front-to-back loading is done. Within
  // the window it's still floor-first with the no-float support rule, so every box
  // rests on the floor or a filled footprint. A windowed box that can't fit its band
  // falls back to anywhere, so a hold with room never false-overflows.
  function packBoxes(grid, boxList) {
    const bays = grid.map(bay => {
      const cells = bay.grids || [];
      return { cells, box: cells.length ? bayBox(cells) : null };
    });
    const occ = bays.map(b => b.cells.map(() => new Set()));
    const key = (x, y, z) => x + "," + y + "," + z;
    let maxH = 0;
    for (const b of bays) for (const c of b.cells) if (c.height > maxH) maxH = c.height;
    const capacity = bays.reduce((a, b) => a + b.cells.reduce((s, c) => s + cellScu(c), 0), 0);

    const fits = (set, c, ox, oy, oz, bw, bl, bh) => {
      if (ox + bw > c.width || oy + bl > c.length || oz + bh > c.height) return false;
      for (let z = oz; z < oz + bh; z++)
        for (let y = oy; y < oy + bl; y++)
          for (let x = ox; x < ox + bw; x++)
            if (set.has(key(x, y, z))) return false;
      if (oz > 0)  // must rest on a fully-occupied footprint below
        for (let y = oy; y < oy + bl; y++)
          for (let x = ox; x < ox + bw; x++)
            if (!set.has(key(x, y, oz - 1))) return false;
      return true;
    };

    const placed = [], overflow = [];
    let placedScu = 0;
    // `win` confines the box to a slice of the access axis (front-to-back banding);
    // `cellSet` (a Set of "bi:ci" ids) confines it to one compartment. Either may be null.
    const place = (item, win, cellSet) => {
      const orients = orientations(item.dims);
      for (let oz = 0; oz < maxH; oz++)
        for (let bi = 0; bi < bays.length; bi++) {
          const { cells, box } = bays[bi];
          for (let ci = 0; ci < cells.length; ci++) {
            if (cellSet && !cellSet.has(bi + ":" + ci)) continue;
            const c = cells[ci], set = occ[bi][ci];
            const dz = c.z - box.minZ, dx = c.x - box.minX;
            for (let oi = 0; oi < orients.length; oi++) {
              const [bw, bl, bh] = orients[oi];
              if (oz + bh > c.height) continue;
              for (let oy = 0; oy + bl <= c.length; oy++) {
                if (win && win.axis === "d" && (dz + oy < win.lo || dz + oy + bl > win.hi)) continue;
                for (let ox = 0; ox + bw <= c.width; ox++) {
                  if (win && win.axis === "w" && (dx + ox < win.lo || dx + ox + bw > win.hi)) continue;
                  if (fits(set, c, ox, oy, oz, bw, bl, bh)) {
                    for (let z = oz; z < oz + bh; z++)
                      for (let y = oy; y < oy + bl; y++)
                        for (let x = ox; x < ox + bw; x++) set.add(key(x, y, z));
                    placed.push({
                      bi, x: dx + ox, y: dz + oy, z: (c.y || 0) + oz,
                      w: bw, l: bl, h: bh, hue: item.hue, scu: item.scu, gid: item.gid, dest: item.dest,
                      label: (item.dest ? item.dest + " · " : "") + `${item.scu} SCU (${bw}×${bl}×${bh})`,
                    });
                    placedScu += item.scu;
                    return true;
                  }
                }
              }
            }
          }
        }
      return false;
    };

    for (const item of boxList) {
      // try each placement option in order (its band → its compartment → spillover →
      // anywhere); packGroups builds the list, by-size packing passes a bare box.
      const tries = item.tries || [{ win: item.win }];
      let done = false;
      for (const t of tries) if (place(item, t.win || null, t.cells || null)) { done = true; break; }
      if (!done) overflow.push(item);
    }
    return { placed, overflow, placedScu, capacity };
  }

  // by-size packing (largest-first), hue per size — used by the test controls.
  function packCargo(grid, counts) {
    const list = [];
    for (const s of [...CARGO_SIZES].sort((a, b) => b.scu - a.scu))
      for (let i = 0; i < (counts[s.scu] || 0); i++) list.push({ dims: s.dims, hue: s.hue, scu: s.scu });
    const r = packBoxes(grid, list);
    const overflow = {};
    for (const it of r.overflow) overflow[it.scu] = (overflow[it.scu] || 0) + 1;
    return { placed: r.placed, overflow, placedScu: r.placedScu, capacity: r.capacity };
  }

  // decompose an SCU total into standard containers, largest-first, with no
  // container larger than maxBox SCU (the mission tier's hard cap; 1 is always
  // allowed so the remainder is always fillable).
  function synthBoxes(scu, maxBox) {
    maxBox = maxBox || Infinity;
    const out = [];
    let rem = scu;
    for (const s of [...CARGO_SIZES].sort((a, b) => b.scu - a.scu)) {
      if (s.scu > maxBox) continue;
      while (rem >= s.scu) { out.push({ dims: s.dims, scu: s.scu }); rem -= s.scu; }
    }
    return out;
  }

  // Per-ship cargo-hatch access (from the one-off hatch survey). Only ships whose
  // hatches lie on ONE axis are listed — those load front-to-back and cargo order
  // matters. Everything else (externals, cargo lifts, multi-side access) is "open":
  // every box is reachable, so packing order is irrelevant and we pack densely.
  //   axis "depth" → fore/aft (hatch at the rear or front); "width" → side-loading.
  //   near = which end the hatch is on; both = hatches at both ends of the axis.
  const SHIP_ACCESS = {
    "A2 Hercules Starlifter": { axis: "depth", near: "rear", both: true },
    "Apollo Medivac": { axis: "depth", near: "rear" },
    "Apollo Triage": { axis: "depth", near: "rear" },
    "Asgard": { axis: "depth", near: "rear" },
    "Avenger Titan": { axis: "depth", near: "rear" },
    "C1 Spirit": { axis: "depth", near: "rear" },
    "C2 Hercules Starlifter": { axis: "depth", near: "rear", both: true },
    "C8 Pisces": { axis: "depth", near: "rear" },
    "C8X Pisces Expedition": { axis: "depth", near: "rear" },
    "Carrack": { axis: "width", near: "left", both: true },
    "Carrack Expedition": { axis: "width", near: "left", both: true },
    "Caterpillar": { axis: "width", near: "left", both: true },
    "Caterpillar Pirate": { axis: "width", near: "left", both: true },
    "Clipper": { axis: "depth", near: "front" },
    "Corsair": { axis: "depth", near: "rear" },
    "Cutlass Blue": { axis: "depth", near: "rear" },
    "Cutlass Red": { axis: "depth", near: "rear" },
    "Cutter": { axis: "depth", near: "rear" },
    "Cutter Rambler": { axis: "depth", near: "rear" },
    "Cutter Scout": { axis: "depth", near: "rear" },
    "Freelancer": { axis: "depth", near: "rear" },
    "Freelancer DUR": { axis: "depth", near: "rear" },
    "Freelancer MAX": { axis: "depth", near: "rear" },
    "Freelancer MIS": { axis: "depth", near: "rear" },
    "Grey's Shiv": { axis: "depth", near: "rear" },
    "Hammerhead": { axis: "depth", near: "rear", both: true },
    "Hermes": { axis: "depth", near: "rear" },
    "Idris-M": { axis: "depth", near: "rear", both: true },
    "Idris-P": { axis: "depth", near: "rear", both: true },
    "Ironclad": { axis: "depth", near: "front" },
    "Ironclad Assault": { axis: "depth", near: "front" },
    "M2 Hercules Starlifter": { axis: "depth", near: "rear", both: true },
    "MPUV Cargo": { axis: "depth", near: "rear" },
    "Mercury Star Runner": { axis: "depth", near: "rear" },
    "Paladin": { axis: "depth", near: "rear" },
    "Polaris": { axis: "width", near: "left", both: true },
    "Reclaimer": { axis: "depth", near: "rear" },
    "Redeemer": { axis: "depth", near: "rear" },
    "Reliant Kore": { axis: "depth", near: "rear" },
    "Reliant Tana": { axis: "depth", near: "rear" },
    "Starfarer": { axis: "depth", near: "rear" },
    "Starfarer Gemini": { axis: "depth", near: "rear" },
    "Starlancer TAC": { axis: "width", near: "left", both: true },
    "Tiburon": { axis: "depth", near: "rear", both: true },
    "Valkyrie": { axis: "depth", near: "rear" },
    "Valkyrie Liberator": { axis: "depth", near: "rear" },
    "Vulture": { axis: "depth", near: "rear" },
    "Zeus Mk II CL": { axis: "depth", near: "rear" },
    "Zeus Mk II ES": { axis: "depth", near: "rear" },
  };
  const OPEN_ACCESS = { open: true };
  // Per-ship hold hints for geometry the size heuristic mis-ranks. `secondary` is a
  // regex on cell name: a compartment whose cells ALL match is spillover/grouping-only
  // (never a primary hold), and EVERY other compartment is primary regardless of size.
  // Hand-curated like SHIP_ACCESS, for ships where adjacent/stacked cells confuse the
  // automatic primary/secondary split.
  //   Ironclad — the secure rooms sit on a catwalk (y=3) inside the tall main holds'
  //   vertical span, so no X/Z/Y rule cleanly isolates them. Only the FORE secure
  //   lockers are "extra" space; the four main quadrants AND the rear secure rooms are
  //   real cargo holds (the fore lockers can still take spillover/grouping cargo).
  const HOLD_HINTS = {
    "Ironclad": { secondary: /^Secure Front/i },
  };
  // Look up a ship's access, tolerant of cargo-DB naming variants (e.g. sc-cargo
  // "C2 Hercules" vs p4k "C2 Hercules Starlifter"): exact, else one name a
  // case-insensitive prefix of the other. Unlisted → open (order doesn't matter).
  // The resolved entry carries its HOLD_HINTS `hint` (if any) for packGroups.
  function accessFor(name) {
    if (!name) return OPEN_ACCESS;
    let key = SHIP_ACCESS[name] ? name : null;
    if (!key) {
      const lc = name.toLowerCase();
      for (const k in SHIP_ACCESS) {
        const kl = k.toLowerCase();
        if (kl === lc || kl.startsWith(lc) || lc.startsWith(kl)) { key = k; break; }
      }
    }
    if (!key) return OPEN_ACCESS;
    return HOLD_HINTS[key] ? { ...SHIP_ACCESS[key], hint: HOLD_HINTS[key] } : SHIP_ACCESS[key];
  }

  // ---- hold geometry: partition cells into physical compartments ----
  // Flatten every cell with a stable "bi:ci" id and BAY-NORMALIZED footprint (the same
  // frame packBoxes/the renderer use), plus its SCU.
  function flattenCells(grid) {
    const out = [];
    grid.forEach((bay, bi) => {
      const list = bay.grids || [];
      if (!list.length) return;
      const box = bayBox(list);
      list.forEach((c, ci) => out.push({
        id: bi + ":" + ci, bi, ci, c, scu: cellScu(c),
        nx0: c.x - box.minX, nx1: c.x - box.minX + c.width,
        nz0: c.z - box.minZ, nz1: c.z - box.minZ + c.length,
      }));
    });
    return out;
  }
  // A compartment is a maximal cluster of cells touching on the X/Z plane (union-find).
  // Cells separated by a gap (a corridor, a deck break) fall into separate compartments,
  // so left/right holds (Hermes, Carrack) and fore/aft holds (C2) are distinct loadable
  // regions — the basis for keeping different destinations physically apart.
  function compartments(cells) {
    const GAP = 1;   // touching/adjacent cells merge; a ≥1-unit gap splits them
    const parent = cells.map((_, i) => i);
    const find = (i) => { while (parent[i] !== i) { parent[i] = parent[parent[i]]; i = parent[i]; } return i; };
    const adj = (a, b) => a.bi === b.bi
      && a.nx0 < b.nx1 + GAP && b.nx0 < a.nx1 + GAP
      && a.nz0 < b.nz1 + GAP && b.nz0 < a.nz1 + GAP;
    for (let i = 0; i < cells.length; i++)
      for (let j = i + 1; j < cells.length; j++)
        if (adj(cells[i], cells[j])) parent[find(i)] = find(j);
    const by = {};
    cells.forEach((cell, i) => {
      const r = find(i);
      const g = by[r] || (by[r] = { cells: [], ids: new Set(), scu: 0 });
      g.cells.push(cell); g.ids.add(cell.id); g.scu += cell.scu;
    });
    return Object.values(by);
  }
  // Capacity profile of one compartment along the access axis (bay-normalized), so a
  // band is sized to the real cross-section where the hold narrows or has a gap.
  function compProfile(comp, axis) {
    const start = (cell) => axis === "width" ? cell.nx0 : cell.nz0;
    const len = (cell) => axis === "width" ? cell.c.width : cell.c.length;
    const cross = (cell) => axis === "width" ? cell.c.length * cell.c.height : cell.c.width * cell.c.height;
    let lo = Infinity, hi = -Infinity;
    for (const cell of comp.cells) { lo = Math.min(lo, start(cell)); hi = Math.max(hi, start(cell) + len(cell)); }
    const prof = new Array(hi - lo).fill(0);
    for (const cell of comp.cells)
      for (let d = start(cell); d < start(cell) + len(cell); d++) prof[d - lo] += cross(cell);
    return { prof, lo, hi };
  }

  // Pack delivery-ordered groups (one per destination) into the hold, choosing a
  // strategy by the ship's hold geometry. Each group's index becomes its boxes' `gid`;
  // boxes carry their own {hue,dest} when set, else the group's.
  //
  //   OPEN   (no access axis): dense floor-first, order irrelevant — every box reachable.
  //   LINEAR (1 primary compartment): one band per destination along the access axis,
  //          hatch-first, so the first delivery sits at the hatch and nothing buries it.
  //   SPLIT  (≥2 primary compartments — left/right or fore/aft holds): whole destinations
  //          assigned to whole compartments (balanced by capacity), banded within each, so
  //          a stop's cargo stays in one physically-separate, independently-reachable hold.
  //
  // Small secondary holds (a closet < SECONDARY_FRAC of the largest — e.g. the Star
  // Runner's 6-SCU side hold) are SPILLOVER-ONLY: filled only once the primaries are full.
  function packGroups(grid, orderedGroups, access) {
    const SLACK = 1.3, SECONDARY_FRAC = 0.25;
    const cells = flattenCells(grid);
    const banded = !!(access && access.axis) && cells.length > 0;

    const boxesOf = (g, gi) => [...g.boxes].sort((a, b) => b.scu - a.scu).map(bx => ({
      dims: bx.dims, scu: bx.scu, gid: gi,
      hue: bx.hue != null ? bx.hue : g.hue,
      dest: bx.dest != null ? bx.dest : g.dest,
    }));

    if (!banded) {                          // OPEN: plain floor-first, any order
      const list = [];
      orderedGroups.forEach((g, gi) => boxesOf(g, gi).forEach(b => { b.tries = [{}]; list.push(b); }));
      const r = packBoxes(grid, list);
      r.strategy = "open";
      return r;
    }

    const comps = compartments(cells);
    // Primary vs spillover-only: a per-ship name hint wins (a compartment whose cells all
    // match is secondary, everything else primary); otherwise rank by size (a hold below
    // SECONDARY_FRAC of the largest is a small secondary that fills only after the rest).
    const hint = access.hint;
    let primary, secondary;
    if (hint && hint.secondary) {
      const isSec = (c) => c.cells.every(cell => hint.secondary.test(cell.c.name || ""));
      secondary = comps.filter(isSec);
      primary = comps.filter(c => !isSec(c));
      if (!primary.length) primary = comps.slice();
    } else {
      const maxCap = Math.max(...comps.map(c => c.scu), 1);
      primary = comps.filter(c => c.scu >= SECONDARY_FRAC * maxCap);
      if (!primary.length) primary = [comps[0]];
      secondary = comps.filter(c => !primary.includes(c));
    }
    const spillIds = new Set();
    secondary.forEach(c => c.ids.forEach(id => spillIds.add(id)));

    const axis = access.axis, wAxis = axis === "width" ? "w" : "d";
    const high = access.near === "front" || access.near === "right";   // hatch at the far end

    // assign each destination to the primary compartment with the most room left, so
    // different stops land in different compartments while the fill stays balanced.
    const rem = primary.map(c => c.scu);
    const assigned = primary.map(() => []);
    orderedGroups.forEach((g, gi) => {
      let best = 0;
      for (let i = 1; i < primary.length; i++) if (rem[i] > rem[best]) best = i;
      assigned[best].push({ g, gi });
      rem[best] -= (g.scu || 0);
    });

    // within each compartment, band its destinations front-to-back from the hatch
    const list = [];
    primary.forEach((comp, ci) => {
      const { prof, lo, hi } = compProfile(comp, axis);
      const span = hi - lo;
      let frontier = 0;
      assigned[ci].forEach(({ g, gi }) => {
        const need = (g.scu || 0) * SLACK;
        let acc = 0, k = 0;
        while (frontier + k < span && acc < need) { acc += prof[high ? span - 1 - (frontier + k) : frontier + k]; k++; }
        if (k === 0) k = 1;
        const win = high
          ? { axis: wAxis, lo: lo + span - frontier - k, hi: lo + span - frontier }
          : { axis: wAxis, lo: lo + frontier, hi: lo + frontier + k };
        frontier += k;
        boxesOf(g, gi).forEach(b => {
          b.tries = [{ cells: comp.ids, win }, { cells: comp.ids }];   // its band, then anywhere in its compartment
          if (spillIds.size) b.tries.push({ cells: spillIds });        // then a secondary hold
          b.tries.push({});                                            // last resort: anywhere
          list.push(b);
        });
      });
    });

    // describe the chosen strategy for the UI: how the primary holds are separated.
    let split = null;
    if (primary.length > 1) {
      const cen = (c, k) => c.cells.reduce((a, x) => a + (k === "w" ? (x.nx0 + x.nx1) : (x.nz0 + x.nz1)) / 2, 0) / c.cells.length;
      const cx = primary.map(c => cen(c, "w")), cz = primary.map(c => cen(c, "z"));
      split = (Math.max(...cx) - Math.min(...cx)) >= (Math.max(...cz) - Math.min(...cz)) ? "width" : "depth";
    }
    const r = packBoxes(grid, list);
    r.strategy = primary.length > 1 ? "split" : "linear";
    r.holds = primary.length;
    r.split = split;
    r.spill = spillIds.size > 0;
    return r;
  }

  // a sized, solid, color-tinted cargo box with 1-SCU grid lines on its faces.
  function packedBoxHtml(b, S, box, flip) {
    const [bx, by] = flipXZ(b.x, b.y, b.w, b.l, box, flip);
    const W = b.w * S, L = b.l * S, H = b.h * S;
    const top = `hsl(${b.hue},66%,57%)`, yw = `hsl(${b.hue},62%,44%)`, xw = `hsl(${b.hue},56%,32%)`;
    const lines = `background-image:linear-gradient(to right,rgba(0,0,0,.22) 1px,transparent 1px),`
      + `linear-gradient(to bottom,rgba(0,0,0,.22) 1px,transparent 1px);background-size:${S}px ${S}px`;
    const st = (bgc, fw, fh) => `width:${fw}px;height:${fh}px;border:1px solid rgba(0,0,0,.5);background-color:${bgc};${lines}`;
    const col = [top, yw, yw, xw, xw];
    const f = boxFaces(W, L, H, (i, fw, fh, tf) =>
      `<div class="cgc" style="${st(col[i], fw, fh)};transform-origin:0 0;transform:${tf}"></div>`);
    return `<div class="cg-box" data-gid="${b.gid == null ? "" : b.gid}" data-dest="${esc(b.dest == null ? "" : b.dest)}" title="${esc(b.label)}" style="transform:translate3d(${bx * S}px,${by * S}px,${b.z * S}px)">${f}</div>`;
  }

  // Flat (non-rotated) overlays placed in cg-scene at the projected position of a
  // 3D point — used for bay-name tags and the forward marker so text stays readable.
  function flatAt(px, py, pz, S, m, cls, text) {
    const [sx, sy] = project(px * S, py * S, pz * S);
    return `<div class="${cls}" style="left:${(m.left + sx).toFixed(1)}px;top:${(m.top + sy).toFixed(1)}px">${text}</div>`;
  }
  function bayTags(cells, box, S, m, opts, flip) {
    let out = "";
    // Label cells biggest-first and DROP any tag that would collide with one already
    // placed — keeps dense layouts (Caterpillar, Ironclad) legible instead of a pile of
    // overlapping labels. The test is an AABB over each tag's ESTIMATED rendered size
    // (10px mono ≈ 6px/char + padding, capped at the max-width), so a long name that
    // overhangs a near neighbour is caught where the old fixed 48px gap missed it.
    const placed = [];
    const tagW = (name) => Math.min(116, name.length * 6 + 14);
    const named = cells.filter(c => c.name)
      .sort((a, b) => (b.width * b.length * b.height) - (a.width * a.length * a.height));
    // Drop a redundant trailing word shared by every bay name (usually the ship name —
    // "Cargo Left Hermes" / "Cargo Right Hermes" → "Cargo Left" / "Cargo Right"), so the
    // tags fit without truncation. Left untouched when the names don't share a tail.
    const labelOf = (() => {
      const ws = named.map(c => c.name.trim().split(/\s+/));
      const last = ws[0] && ws[0][ws[0].length - 1];
      const strip = ws.length > 1 && ws.every(w => w.length > 1 && w[w.length - 1] === last);
      return (c) => strip ? c.name.trim().split(/\s+/).slice(0, -1).join(" ") : c.name;
    })();
    // On a dense hold the tiny secure/EVA sub-grids (e.g. Ironclad's 2×2 cells) are
    // smaller than their own labels, so they just clutter — skip naming them and let the
    // big cargo bays carry the labels. Sparse holds (≤4 named cells) keep every tag.
    const minArea = named.length > 4 ? 9 : 0;
    for (const c of named) {
      if (c.width * c.length < minArea) continue;
      let cw = c.x - box.minX + c.width / 2, cd = c.z - box.minZ + c.length / 2;
      if (flip) { cw = box.w - cw; cd = box.l - cd; }
      const [sx, sy] = project(cw * S, cd * S, ((c.y || 0) + c.height) * S);
      const label = labelOf(c), w = tagW(label);
      if (placed.some(p => Math.abs(p.x - sx) < (p.w + w) / 2 + 2 && Math.abs(p.y - sy) < 15)) continue;
      placed.push({ x: sx, y: sy, w });
      out += `<div class="cg-baytag" style="left:${(m.left + sx).toFixed(1)}px;top:${(m.top + sy).toFixed(1)}px">${esc(label)}</div>`;
    }
    // forward = +z (we map the ship's +Y nose axis to depth); only meaningful when the
    // layout is the real ship geometry, not the synthesised row-tiling. The marker rides
    // the flip too, so it points at the nose wherever the hull is facing.
    if (opts && opts.layout === "deck" && cells.length) {
      const maxZ = Math.max(...cells.map(c => c.z - box.minZ + c.length));
      const xs = cells.map(c => c.x - box.minX), xe = cells.map(c => c.x - box.minX + c.width);
      const midX = (Math.min(...xs) + Math.max(...xe)) / 2;
      let fw = midX, fd = maxZ + 0.6;
      if (flip) { fw = box.w - midX; fd = box.l - (maxZ + 0.6); }
      out += flatAt(fw, fd, 0, S, m, "cg-fwd", "▲ FWD");
    }
    return out;
  }
  function bayWrap(inner, label, box, scu, m, cells, S, opts, flip) {
    const tags = cells ? bayTags(cells, box, S, m, opts, flip) : "";
    return `<div class="cg-bay">
      <div class="cg-scene" style="width:${m.w}px;height:${m.h}px">
        <div class="cg-stage" style="left:${m.left}px;top:${m.top}px;transform:${STAGE_TF}">${inner}</div>
        ${tags}
      </div>
      <div class="cg-baylabel">${label} <span class="cg-baydim">${box.w}×${box.l}</span>
        <span class="cg-bayscu">${num(scu)} SCU</span></div>
    </div>`;
  }

  // grid: bays list. opts.scale = px/SCU. opts.segments (fill mode) =
  // [{label, scu, hue}] in load order; absent → volume mode.
  function cargoGridHtml(grid, opts) {
    opts = opts || {};
    const S = opts.scale || 18;
    if (!grid || !grid.length) return opts.empty || "";
    const total = grid.length;
    // Rear-hatch ships are shown yawed 180° so the hatch (loading door) faces the
    // viewer — the end where cargo goes in/out, and where the first delivery sits.
    const access = opts.access;
    const flip = !!(access && access.axis === "depth" && access.near === "rear" && !access.both);

    if (opts.packed) {
      // floor per sub-grid + the packer's placed sized boxes, grouped by bay
      const byBay = grid.map(() => []);
      for (const b of opts.packed.placed) byBay[b.bi].push(b);
      const bays = grid.map((bay, i) => {
        const cells = bay.grids || [];
        if (!cells.length) return "";
        const box = bayBox(cells);
        const inner = cells.map(c => floorHtml(c, box, S, flip)).join("")
          + byBay[i].map(b => packedBoxHtml(b, S, box, flip)).join("");
        return bayWrap(inner, total > 1 ? `Bay ${i + 1}` : "Hold", box,
          cells.reduce((a, c) => a + cellScu(c), 0), sceneMetrics(cells, box, S, flip), cells, S, opts, flip);
      }).join("");
      return `<div class="cg-wrap">${bays}</div>`;
    }

    if (!opts.segments) {
      const bays = grid.map((bay, i) => {
        const cells = bay.grids || [];
        if (!cells.length) return "";
        const box = bayBox(cells);
        return bayWrap(cells.map(c => volumeBoxHtml(c, box, S, flip)).join(""),
          total > 1 ? `Bay ${i + 1}` : "Hold", box,
          cells.reduce((a, c) => a + cellScu(c), 0), sceneMetrics(cells, box, S, flip), cells, S, opts, flip);
      }).join("");
      return `<div class="cg-wrap">${bays}</div>`;
    }

    // fill mode: a flat color array (one hue per loaded SCU), then drop cubes
    // into the global load order across bays.
    const colors = [];
    for (const seg of opts.segments)
      for (let k = 0; k < seg.scu; k++) colors.push({ hue: seg.hue, label: seg.label });

    let gi = 0;
    const bays = grid.map((bay, i) => {
      const cells = bay.grids || [];
      if (!cells.length) return "";
      const box = bayBox(cells);
      const floors = cells.map(c => floorHtml(c, box, S, flip)).join("");
      const order = fillOrder(cells, box);
      const cubes = [];
      for (const p of order) {
        if (gi >= colors.length) break;
        const cx = flip ? box.w - p.x - 1 : p.x, cy = flip ? box.l - p.y - 1 : p.y;
        cubes.push(cubeHtml(cx, cy, p.z, colors[gi].hue, S, colors[gi].label));
        gi++;
      }
      return bayWrap(floors + cubes.join(""),
        total > 1 ? `Bay ${i + 1}` : "Hold", box,
        cells.reduce((a, c) => a + cellScu(c), 0), sceneMetrics(cells, box, S, flip), cells, S, opts, flip);
    }).join("");
    return `<div class="cg-wrap">${bays}</div>`;
  }

  global.cargoGridHtml = cargoGridHtml;
  global.packCargo = packCargo;
  global.packGroups = packGroups;
  global.accessFor = accessFor;
  global.synthBoxes = synthBoxes;
  global.CARGO_SIZES = CARGO_SIZES;
  // Node (test runner) only: also expose the pure packing helpers for unit tests.
  // The `module` guard is false in a browser <script>, so this is a no-op there.
  if (typeof module !== "undefined" && module.exports)
    module.exports = { cargoGridHtml, packCargo, packGroups, accessFor, synthBoxes,
                       orientations, packBoxes, CARGO_SIZES };
})(typeof window !== "undefined" ? window : globalThis);
