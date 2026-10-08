import { Focus, Minus, Plus, RotateCcw, Maximize2, Minimize2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useExploration } from "@/context/ExplorationContext";
import { nodeLabel } from "@/utils/adapter";
import { NodeDetails } from "@/components/Graph/NodeDetails";

function kind(n: any) {
  const s = (n.labels?.join(" ") || n.type || "").toLowerCase();
  if (s.includes("plant name") || s === "plant") return "plant";
  if (s.includes("plant part") || s === "plant_part") return "part";
  if (s.includes("phytochemical") || s === "phytochemical") return "compound";
  if (s.includes("therapeutic") || s === "therapeutic_use") return "use";
  if (s.includes("bio-activity") || s.includes("bioactivity") || s === "bio_activity") return "activity";
  if (s.includes("target")) return "target";
  if (s.includes("document")) return "document";
  return "other";
}

const palette: Record<string, { fill: string; r: number; label: string }> = {
  plant: { fill: "#244c38", r: 28, label: "Plant" },
  part: { fill: "#6f8a6a", r: 21, label: "Plant part" },
  compound: { fill: "#c08a34", r: 17, label: "Phytochemical" },
  use: { fill: "#9c9a75", r: 14, label: "Therapeutic use" },
  activity: { fill: "#82635c", r: 15, label: "Bio-activity" },
  target: { fill: "#4f6f88", r: 16, label: "Target" },
  document: { fill: "#7c7c70", r: 13, label: "Document" },
  other: { fill: "#7c7c70", r: 13, label: "Other" },
};
const alwaysLabel = new Set(["plant", "part"]);
const MIN_Z = 0.35;
const MAX_Z = 2.8;
const clampZ = (z: number) => Math.min(MAX_Z, Math.max(MIN_Z, z));
const EASE = "transform 420ms cubic-bezier(.22,1,.36,1)";

type ViewT = { x: number; y: number; z: number };
type EdgeRef = { relId: string; role: "source" | "target" };

export function GraphExplorer() {
  const { vm, selection, selectNode, clearSelection, highlightedNodeIds } = useExploration();
  const svgRef = useRef<SVGSVGElement | null>(null);
  const gRef = useRef<SVGGElement | null>(null);
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const nodes = vm.graph.nodes;
  const rels = vm.graph.relationships;

  const [fullscreen, setFullscreen] = useState(false);
  const [size, setSize] = useState({ w: 1100, h: 560 });
  const W = size.w, H = size.h;

  // Responsive canvas - also what makes fullscreen actually useful, instead of
  // just blowing up a fixed 1100x560 viewBox with letterboxing on either side.
  useEffect(() => {
    const el = canvasRef.current;
    if (!el) return;
    const ro = new ResizeObserver(entries => {
      const r = entries[0]?.contentRect;
      if (r && r.width > 0 && r.height > 0) setSize({ w: Math.round(r.width), h: Math.round(r.height) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    if (!fullscreen) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setFullscreen(false); };
    window.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { window.removeEventListener("keydown", onKey); document.body.style.overflow = prevOverflow; };
  }, [fullscreen]);

  // Live pan/zoom lives in a ref and is painted straight to the DOM during a gesture,
  // so dragging/scrolling never triggers a React re-render of the node/edge list.
  const liveView = useRef<ViewT>({ x: 0, y: 0, z: 1 });
  const [view, setViewState] = useState<ViewT>({ x: 0, y: 0, z: 1 });
  const drag = useRef<{ x: number; y: number; moved: boolean } | null>(null);
  const [typeFilter, setTypeFilter] = useState<Set<string>>(new Set());

  const paint = useCallback((v: ViewT, animate: boolean) => {
    liveView.current = v;
    if (gRef.current) {
      gRef.current.style.transition = animate ? EASE : "none";
      gRef.current.style.transform = `translate(${v.x}px, ${v.y}px) scale(${v.z})`;
    }
  }, []);
  const goTo = useCallback((next: ViewT) => {
    const v = { ...next, z: clampZ(next.z) };
    paint(v, true);
    setViewState(v);
  }, [paint]);
  useEffect(() => paint(view, false), [W, H]); // eslint-disable-line react-hooks/exhaustive-deps

  // Base layout (BFS layered, same as before) - untouched positions the graph starts at.
  const basePos = useMemo(() => {
    const out = new Map<string, { x: number; y: number }>();
    if (!nodes.length) return out;
    const root = nodes.find(n => kind(n) === "plant") || nodes[0];
    const adj = new Map<string, string[]>();
    nodes.forEach(n => adj.set(n.id, []));
    rels.forEach(r => { adj.get(r.source)?.push(r.target); adj.get(r.target)?.push(r.source); });
    const dist = new Map<string, number>([[root.id, 0]]);
    const queue = [root.id];
    while (queue.length) {
      const id = queue.shift()!;
      for (const next of adj.get(id) || []) {
        if (!dist.has(next)) { dist.set(next, (dist.get(id) || 0) + 1); queue.push(next); }
      }
    }
    nodes.forEach(n => { if (!dist.has(n.id)) dist.set(n.id, Math.max(...Array.from(dist.values()), 0) + 1); });
    const layers = new Map<number, any[]>();
    nodes.forEach(n => { const d = dist.get(n.id) || 0; if (!layers.has(d)) layers.set(d, []); layers.get(d)!.push(n); });
    const maxDepth = Math.max(...Array.from(layers.keys()), 1);
    for (const [d, arr] of layers) {
      arr.sort((a, b) => { const ak = kind(a), bk = kind(b); return ak !== bk ? ak.localeCompare(bk) : nodeLabel(a).localeCompare(nodeLabel(b)); });
      const x = 80 + d * ((W - 160) / maxDepth);
      const cols = Math.max(1, Math.ceil(Math.sqrt(arr.length / 1.8)));
      const rows = Math.ceil(arr.length / cols);
      arr.forEach((n, i) => {
        const col = i % cols, row = Math.floor(i / cols);
        const localWidth = d === 0 ? 0 : Math.min(280, (cols - 1) * 90);
        const startX = x - localWidth / 2;
        const px = d === 0 ? 80 : startX + (cols === 1 ? 0 : (col / (cols - 1)) * localWidth);
        const py = 55 + (rows === 1 ? 0 : (row / (rows - 1)) * (H - 110));
        out.set(n.id, { x: px, y: py });
      });
    }
    return out;
  }, [nodes, rels, W, H]);

  // Nodes the user has manually dragged (Neo4j-style). Kept in a ref during the
  // gesture for direct DOM manipulation, committed to state (so it survives
  // re-renders and selection changes) only on pointer-up.
  const overridesRef = useRef(new Map<string, { x: number; y: number }>());
  const [overrideTick, setOverrideTick] = useState(0);
  const pos = useMemo(() => {
    const m = new Map(basePos);
    overridesRef.current.forEach((v, k) => m.set(k, v));
    return m;
  }, [basePos, overrideTick]);

  useEffect(() => {
    if (!selection.nodeId) return;
    const p = pos.get(selection.nodeId);
    if (p) goTo({ x: W / 2 - p.x * liveView.current.z, y: H / 2 - p.y * liveView.current.z, z: liveView.current.z });
  }, [selection.nodeId]); // eslint-disable-line react-hooks/exhaustive-deps

  const shownIds = useMemo(() => {
    const connected = new Set<string>();
    if (selection.nodeId) rels.forEach(r => {
      if (r.source === selection.nodeId || r.target === selection.nodeId) { connected.add(r.source); connected.add(r.target); }
    });
    return highlightedNodeIds.size ? highlightedNodeIds : selection.nodeId ? connected : new Set(nodes.map(n => n.id));
  }, [selection.nodeId, highlightedNodeIds, rels, nodes]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    nodes.forEach(n => { const t = kind(n); c[t] = (c[t] || 0) + 1; });
    return c;
  }, [nodes]);
  const toggleType = (t: string) => setTypeFilter(prev => { const next = new Set(prev); next.has(t) ? next.delete(t) : next.add(t); return next; });
  const nodesById = useMemo(() => new Map(nodes.map(n => [n.id, n])), [nodes]);

  // node -> list of edges touching it, so a node drag only has to touch its own edges.
  const edgesByNode = useMemo(() => {
    const m = new Map<string, EdgeRef[]>();
    rels.forEach(r => {
      (m.get(r.source) ?? m.set(r.source, []).get(r.source)!).push({ relId: r.id, role: "source" });
      (m.get(r.target) ?? m.set(r.target, []).get(r.target)!).push({ relId: r.id, role: "target" });
    });
    return m;
  }, [rels]);
  const nodeEls = useRef(new Map<string, SVGGElement>());
  const lineEls = useRef(new Map<string, SVGLineElement>());

  // Direct 1-hop adjacency (undirected), used so dragging a node gives its
  // immediate neighbors a gentle elastic pull, the way Neo4j's link force does.
  const adjacency = useMemo(() => {
    const m = new Map<string, string[]>();
    rels.forEach(r => {
      (m.get(r.source) ?? m.set(r.source, []).get(r.source)!).push(r.target);
      (m.get(r.target) ?? m.set(r.target, []).get(r.target)!).push(r.source);
    });
    return m;
  }, [rels]);
  const PULL = 0.22; // how strongly neighbors follow a dragged node (0-1)
  const displaced = useRef(new Map<string, { x: number; y: number }>());
  const settleGen = useRef(0);

  const moveNodeAndEdges = (id: string, x: number, y: number) => {
    const g = nodeEls.current.get(id);
    if (g) g.setAttribute("transform", `translate(${x} ${y})`);
    (edgesByNode.get(id) || []).forEach(({ relId, role }) => {
      const line = lineEls.current.get(relId);
      if (!line) return;
      if (role === "source") { line.setAttribute("x1", String(x)); line.setAttribute("y1", String(y)); }
      else { line.setAttribute("x2", String(x)); line.setAttribute("y2", String(y)); }
    });
  };

  // Eases displaced neighbors back to their resting layout position after a drop -
  // the "spring settle" that makes the whole cluster feel alive, not just dragged.
  const settleNeighbors = (ids: string[]) => {
    const gen = ++settleGen.current;
    const start = performance.now();
    const duration = 420;
    const from = new Map(ids.map(id => [id, displaced.current.get(id) || pos.get(id)!]));
    const to = new Map(ids.map(id => [id, pos.get(id)!]));
    const tick = (now: number) => {
      if (gen !== settleGen.current) return; // a newer drag/settle superseded this one
      const t = Math.min(1, (now - start) / duration);
      const e = 1 - Math.pow(1 - t, 3);
      ids.forEach(id => {
        const s = from.get(id)!, tg = to.get(id)!;
        moveNodeAndEdges(id, s.x + (tg.x - s.x) * e, s.y + (tg.y - s.y) * e);
      });
      if (t < 1) requestAnimationFrame(tick);
      else ids.forEach(id => displaced.current.delete(id));
    };
    requestAnimationFrame(tick);
  };

  const screenToWorld = (clientX: number, clientY: number) => {
    const svg = svgRef.current;
    if (!svg) return { x: 0, y: 0 };
    const pt = svg.createSVGPoint();
    pt.x = clientX; pt.y = clientY;
    const ctm = svg.getScreenCTM();
    if (!ctm) return { x: 0, y: 0 };
    return pt.matrixTransform(ctm.inverse());
  };

  // --- background pan ---
  const onPointerDown = (e: React.PointerEvent) => {
    (e.target as Element).setPointerCapture?.(e.pointerId);
    drag.current = { x: e.clientX, y: e.clientY, moved: false };
  };
  const onPointerMove = (e: React.PointerEvent) => {
    if (!drag.current) return;
    const dx = e.clientX - drag.current.x, dy = e.clientY - drag.current.y;
    if (Math.abs(dx) + Math.abs(dy) > 2) drag.current.moved = true;
    paint({ ...liveView.current, x: liveView.current.x + dx, y: liveView.current.y + dy }, false);
    drag.current.x = e.clientX; drag.current.y = e.clientY;
  };
  const endDrag = () => { if (!drag.current) return; setViewState(liveView.current); drag.current = null; };
  const onBackgroundClick = () => { if (!drag.current?.moved) clearSelection(); };
  const onWheel = (e: React.WheelEvent) => {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.08 : 1 / 1.08;
    const newZ = clampZ(liveView.current.z * factor);
    const p = screenToWorld(e.clientX, e.clientY);
    const worldX = (p.x - liveView.current.x) / liveView.current.z;
    const worldY = (p.y - liveView.current.y) / liveView.current.z;
    const v = { x: p.x - worldX * newZ, y: p.y - worldY * newZ, z: newZ };
    paint(v, false);
    setViewState(v);
  };
  const focusNode = (id: string) => {
    const p = pos.get(id);
    if (p) goTo({ x: W / 2 - p.x * 1.4, y: H / 2 - p.y * 1.4, z: 1.4 });
  };

  // --- individual node drag (Neo4j-style) ---
  const nodeDrag = useRef<{ id: string; startX: number; startY: number; origX: number; origY: number; curX: number; curY: number; moved: boolean; neighbors: string[] } | null>(null);
  const justDragged = useRef(false);

  const onNodePointerDown = (e: React.PointerEvent, id: string) => {
    e.stopPropagation();
    (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
    settleGen.current++; // cancel any in-flight settle so it can't fight this new drag
    const p = pos.get(id)!;
    const neighbors = Array.from(new Set(adjacency.get(id) || [])).filter(nid => nid !== id);
    nodeDrag.current = { id, startX: e.clientX, startY: e.clientY, origX: p.x, origY: p.y, curX: p.x, curY: p.y, moved: false, neighbors };
  };
  const onNodePointerMove = (e: React.PointerEvent) => {
    const d = nodeDrag.current;
    if (!d) return;
    e.stopPropagation();
    const dx = (e.clientX - d.startX) / liveView.current.z;
    const dy = (e.clientY - d.startY) / liveView.current.z;
    if (Math.abs(dx) + Math.abs(dy) > 2) d.moved = true;
    d.curX = d.origX + dx; d.curY = d.origY + dy;
    moveNodeAndEdges(d.id, d.curX, d.curY);
    // Elastic pull: direct neighbors follow a fraction of the drag so the
    // connected cluster moves together, rather than the graph staying rigid.
    d.neighbors.forEach(nid => {
      const base = pos.get(nid);
      if (!base) return;
      const nx = base.x + dx * PULL, ny = base.y + dy * PULL;
      displaced.current.set(nid, { x: nx, y: ny });
      moveNodeAndEdges(nid, nx, ny);
    });
  };
  const onNodePointerUp = (e: React.PointerEvent) => {
    const d = nodeDrag.current;
    if (!d) return;
    e.stopPropagation();
    if (d.moved) {
      overridesRef.current.set(d.id, { x: d.curX, y: d.curY });
      setOverrideTick(t => t + 1);
      justDragged.current = true;
      requestAnimationFrame(() => { justDragged.current = false; });
      // Neo4j-style settle: a quick elastic bounce on the dropped node so the
      // release reads as a physical "snap into place" rather than a dead stop.
      const circle = nodeEls.current.get(d.id)?.querySelector(".gx-circle") as SVGCircleElement | null;
      if (circle) {
        circle.classList.remove("graph-node-drop");
        void circle.getBoundingClientRect();
        circle.classList.add("graph-node-drop");
      }
      if (d.neighbors.length) settleNeighbors(d.neighbors);
    }
    nodeDrag.current = null;
  };
  const onNodeClick = (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    if (justDragged.current) return;
    selectNode(id);
  };
  const resetLayout = () => { overridesRef.current.clear(); setOverrideTick(t => t + 1); goTo({ x: 0, y: 0, z: 1 }); };

  return (
    <div className={fullscreen
      ? "fixed inset-0 z-50 flex flex-col overflow-hidden rounded-none border-0 bg-[#edf1e7] shadow-2xl animate-[gx-pop_.22s_ease]"
      : "overflow-hidden rounded-2xl border border-specimen bg-[#edf1e7] shadow-specimen"}>
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-specimen/70 bg-parchment2/90 px-4 py-3">
        <div>
          <p className="specimen-label">Knowledge graph</p>
          <p className="text-sm text-ink/60">{nodes.length} nodes · {rels.length} relationships</p>
        </div>
        <div className="flex items-center gap-1">
          <button className="graphbtn" title="Zoom in" onClick={() => goTo({ ...liveView.current, z: liveView.current.z * 1.15 })}><Plus size={16} /></button>
          <button className="graphbtn" title="Zoom out" onClick={() => goTo({ ...liveView.current, z: liveView.current.z / 1.15 })}><Minus size={16} /></button>
          <button className="graphbtn" title="Reset layout &amp; view" onClick={resetLayout}><RotateCcw size={15} /></button>
          <button className="graphbtn" title="Fit" onClick={() => goTo({ x: 0, y: 0, z: .86 })}><Focus size={15} /></button>
          <span className="mx-1 h-5 w-px bg-specimen" />
          <button className="graphbtn" title={fullscreen ? "Exit full screen (Esc)" : "Full screen"} onClick={() => setFullscreen(f => !f)}>
            {fullscreen ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
          </button>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-1.5 border-b border-specimen/60 bg-parchment2/60 px-4 py-2">
        {Object.entries(counts).map(([t, n]) => (
          <button key={t} onClick={() => toggleType(t)}
            className="flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-medium transition-all duration-200 active:scale-95"
            style={{
              borderColor: typeFilter.has(t) ? palette[t].fill : "#d8d0bd",
              background: typeFilter.has(t) ? `${palette[t].fill}1a` : "transparent",
              color: typeFilter.has(t) ? palette[t].fill : "#5f755f",
              opacity: typeFilter.size && !typeFilter.has(t) ? .55 : 1,
            }}>
            <span className="inline-block h-2 w-2 rounded-full" style={{ background: palette[t].fill }} />
            {palette[t].label} <span className="opacity-60">{n}</span>
          </button>
        ))}
        {typeFilter.size > 0 && (
          <button onClick={() => setTypeFilter(new Set())} className="ml-1 text-[11px] text-turmeric underline underline-offset-2">Clear filter</button>
        )}
        {overrideTick > 0 && (
          <span className="ml-auto text-[11px] text-ink/40">Custom layout · <button onClick={resetLayout} className="text-turmeric underline underline-offset-2">reset</button></span>
        )}
      </div>

      <div className={fullscreen ? "flex min-h-0 flex-1" : ""}>
        <div ref={canvasRef}
          className={fullscreen ? "relative min-w-0 flex-1 touch-none overflow-hidden select-none" : "relative h-[520px] touch-none overflow-hidden select-none"}
          style={{ background: "radial-gradient(ellipse at 50% 40%, #f2f5ec 0%, #e7ece0 100%)" }}
          onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={endDrag}
          onPointerLeave={endDrag} onPointerCancel={endDrag} onWheel={onWheel} onClick={onBackgroundClick}>
          <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} className="h-full w-full cursor-grab active:cursor-grabbing">
            <defs>
              <marker id="vana-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
                <path d="M 0 0 L 10 5 L 0 10 z" fill="#72816f" />
              </marker>
              <filter id="vana-node-glow" x="-60%" y="-60%" width="220%" height="220%">
                <feDropShadow dx="0" dy="1" stdDeviation="3" floodColor="#173d2d" floodOpacity="0.35" />
              </filter>
            </defs>
            <g ref={gRef} style={{ transformOrigin: "0 0" }}>
              {rels.map((r, i) => {
                const a = pos.get(r.source), b = pos.get(r.target);
                if (!a || !b) return null;
                const relevant = !highlightedNodeIds.size || (shownIds.has(r.source) && shownIds.has(r.target));
                const typeOk = !typeFilter.size || nodes.some(n => n.id === r.source && typeFilter.has(kind(n))) || nodes.some(n => n.id === r.target && typeFilter.has(kind(n)));
                const dim = (highlightedNodeIds.size && !relevant) || (typeFilter.size && !typeOk);
                return (
                  <line key={r.id} ref={el => { if (el) lineEls.current.set(r.id, el); }}
                    x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="#72816f"
                    strokeWidth={relevant ? 1.5 : 1} strokeOpacity={dim ? .05 : .28} markerEnd="url(#vana-arrow)"
                    className="graph-edge graph-edge-enter" style={{ animationDelay: `${Math.min(i * 4, 400)}ms` }} />
                );
              })}
              {nodes.map((n, i) => {
                const p = pos.get(n.id)!;
                const t = kind(n), st = palette[t];
                const active = selection.nodeId === n.id || highlightedNodeIds.has(n.id);
                const dim = (highlightedNodeIds.size && !active) || (typeFilter.size && !typeFilter.has(t));
                const label = nodeLabel(n);
                const showLabel = alwaysLabel.has(t) || active;
                return (
                  <g key={n.id} ref={el => { if (el) nodeEls.current.set(n.id, el); }}
                    transform={`translate(${p.x} ${p.y})`}
                    className="graph-node graph-node-enter cursor-grab active:cursor-grabbing"
                    style={{ opacity: dim ? .16 : 1, animationDelay: `${Math.min(i * 6, 480)}ms` }}
                    onPointerDown={e => onNodePointerDown(e, n.id)}
                    onPointerMove={onNodePointerMove}
                    onPointerUp={onNodePointerUp}
                    onPointerCancel={onNodePointerUp}
                    onClick={e => onNodeClick(e, n.id)}
                    onDoubleClick={e => { e.stopPropagation(); focusNode(n.id); }}>
                    {active && <circle r={st.r + 7} fill="none" stroke={st.fill} strokeWidth={2} className="animate-pulse-ring" />}
                    <circle className="gx-circle" r={st.r} fill={st.fill} stroke={active ? "#f2c36b" : "#f8f4ea"} strokeWidth={active ? 4 : 1.5}
                      filter={active ? "url(#vana-node-glow)" : undefined} />
                    {showLabel && (
                      <text y={st.r + 15} textAnchor="middle" fontSize={t === "plant" ? 14 : t === "part" ? 11 : 9}
                        fontWeight={t === "plant" ? 600 : 400} fill="#263028" className="select-none pointer-events-none">
                        {label.length > 30 ? `${label.slice(0, 30)}…` : label}
                      </text>
                    )}
                    <g className="graph-tooltip pointer-events-none">
                      <rect x={-130} y={-st.r - 40} width="260" height="30" rx="7" fill="#fbf8f1" stroke="#d8d0bd" />
                      <text textAnchor="middle" y={-st.r - 21} fontSize="10" fill="#173d2d">
                        {label.length > 40 ? `${label.slice(0, 40)}…` : label}
                      </text>
                    </g>
                  </g>
                );
              })}
            </g>
          </svg>
          <div className="pointer-events-none absolute bottom-3 left-3 right-3 flex flex-wrap items-center gap-2 text-[10px] text-ink/50">
            <span className="rounded-full bg-parchment2/90 px-2 py-1 backdrop-blur">Drag canvas to pan · scroll to zoom</span>
            <span className="rounded-full bg-parchment2/90 px-2 py-1 backdrop-blur">Drag a node — connected nodes follow · double-click to focus</span>
            <span className="ml-auto rounded-full bg-parchment2/90 px-2 py-1 backdrop-blur"><Maximize2 size={10} className="mr-1 inline" />Click a node to inspect</span>
          </div>
        </div>

        {/* Full screen only: the node-details / evidence card lives inline here since
            the page's own side panel is out of view behind this overlay. */}
        {fullscreen && (
          <AnimatePresence>
            {selection.nodeId && (
              <motion.div key="fs-details"
                initial={{ opacity: 0, x: 24 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: 24 }}
                transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
                className="w-[380px] shrink-0 overflow-y-auto border-l border-specimen/70 bg-parchment px-4 py-4">
                <NodeDetails
                  node={nodesById.get(selection.nodeId) || nodes[0]}
                  relationships={rels}
                  nodesById={nodesById}
                  onClose={clearSelection}
                  onSelectNode={selectNode}
                />
              </motion.div>
            )}
          </AnimatePresence>
        )}
      </div>
    </div>
  );
}
