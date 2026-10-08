import { ArrowDown, ArrowUp, Database, Leaf, Link2, X } from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { nodeLabel } from "@/utils/adapter";
import type { NodeVM, RelVM } from "@/utils/adapter";

function formatValue(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function friendlyType(node: NodeVM): string {
  const labels = node.labels?.length ? node.labels : [node.type];
  return labels.join(" · ").replace(/_/g, " ");
}

export function NodeDetails({
  node,
  relationships,
  nodesById,
  onClose,
  onSelectNode,
}: {
  node: NodeVM;
  relationships: RelVM[];
  nodesById: Map<string, NodeVM>;
  onClose: () => void;
  onSelectNode: (id: string) => void;
}) {
  const connected = relationships
    .filter((r) => r.source === node.id || r.target === node.id)
    .map((r) => {
      const outgoing = r.source === node.id;
      const neighborId = outgoing ? r.target : r.source;
      const neighbor = nodesById.get(neighborId);
      return { r, outgoing, neighbor };
    });

  const properties = Object.entries(node.properties ?? {}).filter(([, value]) => value !== null && value !== undefined && value !== "");

  return (
    <AnimatePresence mode="wait">
      <motion.div
        key={node.id}
        initial={{ opacity: 0, x: 18, scale: 0.98 }}
        animate={{ opacity: 1, x: 0, scale: 1 }}
        exit={{ opacity: 0, x: -10, scale: 0.98 }}
        transition={{ duration: 0.32, ease: [0.22, 1, 0.36, 1] }}
        className="rounded-2xl border border-specimen bg-parchment2 p-5 shadow-specimen"
      >
      <div className="flex items-start gap-3">
        <span className="rounded-full bg-sage/50 p-2.5 text-canopy">
          <Leaf size={17} />
        </span>
        <div className="min-w-0 flex-1">
          <p className="specimen-label">Selected node</p>
          <h3 className="mt-1 break-words font-display text-2xl text-canopy2">{nodeLabel(node)}</h3>
          <p className="mt-1 text-xs capitalize text-ink/50">{friendlyType(node)}</p>
        </div>
        <button className="graphbtn" title="Close node details" onClick={onClose}>
          <X size={15} />
        </button>
      </div>

      <div className="mt-5 rounded-xl border border-specimen/80 bg-parchment px-3 py-2.5">
        <div className="flex items-center gap-2 text-xs text-ink/55">
          <Database size={13} />
          <span className="font-mono break-all">Node ID: {node.id}</span>
        </div>
      </div>

      <div className="mt-5">
        <div className="mb-2 flex items-center gap-2">
          <p className="specimen-label">Node details</p>
          <span className="text-[10px] text-ink/40">{properties.length} fields</span>
        </div>
        {properties.length === 0 ? (
          <p className="text-sm text-ink/55">No additional properties were returned for this node.</p>
        ) : (
          <div className="space-y-2">
            {properties.map(([key, value], i) => (
              <div
                key={key}
                style={{ animationDelay: `${Math.min(i * 35, 300)}ms` }}
                className="detail-row-enter rounded-xl border border-specimen/70 bg-parchment px-3 py-2.5 transition hover:border-canopy/40"
              >
                <div className="text-[10px] font-semibold uppercase tracking-[0.15em] text-ink/40">{key.replace(/_/g, " ")}</div>
                <div className="mt-1 whitespace-pre-wrap break-words text-sm leading-5 text-ink/75">{formatValue(value)}</div>
              </div>
            ))}
          </div>
        )}
      </div>

      {connected.length > 0 && (
        <div className="mt-5">
          <div className="mb-2 flex items-center gap-2">
            <Link2 size={14} className="text-canopy" />
            <p className="specimen-label">Connected graph entities</p>
            <span className="text-[10px] text-ink/40">{connected.length} relationships</span>
          </div>
          <div className="max-h-64 space-y-2 overflow-y-auto pr-1">
            {connected.map(({ r, outgoing, neighbor }, i) => (
              <button
                key={r.id}
                onClick={() => neighbor && onSelectNode(neighbor.id)}
                disabled={!neighbor}
                style={{ animationDelay: `${Math.min(i * 35, 300)}ms` }}
                className="detail-row-enter flex w-full items-start gap-2 rounded-xl border border-specimen/70 bg-parchment px-3 py-2 text-left transition hover:-translate-y-px hover:border-canopy/50 hover:shadow-sm active:scale-[.98] disabled:cursor-default disabled:opacity-70"
              >
                <span className="mt-0.5 shrink-0 text-canopy">
                  {outgoing ? <ArrowDown size={14} /> : <ArrowUp size={14} />}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block text-[10px] uppercase tracking-[0.13em] text-ink/40">{r.type || "relationship"}</span>
                  <span className="mt-0.5 block break-words text-sm text-canopy2">{neighbor ? nodeLabel(neighbor) : r.source === node.id ? r.target : r.source}</span>
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="mt-5 rounded-xl bg-sage/25 px-3 py-2.5 text-xs leading-5 text-ink/55">
        Selecting a connected entity moves the graph focus to that node while keeping the rest of the evidence and answer unchanged.
      </div>
      </motion.div>
    </AnimatePresence>
  );
}
