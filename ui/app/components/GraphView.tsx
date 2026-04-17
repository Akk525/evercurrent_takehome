'use client';

import { useState, useEffect, useRef, useCallback } from 'react';
import { fetchGraph } from '../lib/api';

// --- Types ---

export interface GraphNode {
  id: string;
  title: string;
  signal_level: 'high' | 'medium' | 'low';
  event_type: string;
  centrality: number;
  upstream_count: number;
  downstream_count: number;
  issue_cluster_id: string | null;
}

export interface GraphEdge {
  source: string;
  target: string;
  relation_type: 'blocks' | 'depends_on' | 'related_to' | 'impacts';
  confidence: number;
  explanation: string;
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

// --- Mock fallback ---

const MOCK_GRAPH_DATA: GraphData = {
  nodes: [
    { id: 'ev_001', title: 'Motor controller blocker', signal_level: 'high', event_type: 'blocker', centrality: 0.9, upstream_count: 0, downstream_count: 3, issue_cluster_id: 'cluster_1' },
    { id: 'ev_002', title: 'PCB layout decision', signal_level: 'high', event_type: 'decision', centrality: 0.7, upstream_count: 1, downstream_count: 2, issue_cluster_id: 'cluster_1' },
    { id: 'ev_003', title: 'Firmware integration risk', signal_level: 'medium', event_type: 'risk', centrality: 0.5, upstream_count: 2, downstream_count: 1, issue_cluster_id: 'cluster_2' },
    { id: 'ev_004', title: 'Supplier delay update', signal_level: 'medium', event_type: 'status_update', centrality: 0.4, upstream_count: 1, downstream_count: 0, issue_cluster_id: 'cluster_2' },
    { id: 'ev_005', title: 'Test coverage gap', signal_level: 'low', event_type: 'noise', centrality: 0.2, upstream_count: 0, downstream_count: 0, issue_cluster_id: null },
  ],
  edges: [
    { source: 'ev_001', target: 'ev_002', relation_type: 'blocks', confidence: 0.8, explanation: 'Shared PCB entities' },
    { source: 'ev_002', target: 'ev_003', relation_type: 'depends_on', confidence: 0.65, explanation: 'Same issue cluster' },
    { source: 'ev_003', target: 'ev_004', relation_type: 'related_to', confidence: 0.5, explanation: 'Shared topic: supply chain' },
    { source: 'ev_001', target: 'ev_003', relation_type: 'impacts', confidence: 0.35, explanation: 'Blocker affecting firmware work' },
  ],
};

// --- Visual constants ---

const SIGNAL_COLORS: Record<string, string> = {
  high: '#E01E5A',
  medium: '#ECB22E',
  low: '#97A0AF',
};

const EDGE_STYLES: Record<string, { stroke: string; dashArray: string }> = {
  blocks:     { stroke: '#E01E5A', dashArray: '6 3' },
  depends_on: { stroke: '#36C5F0', dashArray: 'none' },
  related_to: { stroke: '#97A0AF', dashArray: '2 4' },
  impacts:    { stroke: '#E8A838', dashArray: 'none' },
};

const ALL_RELATION_TYPES = ['blocks', 'depends_on', 'related_to', 'impacts'] as const;

// --- Layout helpers ---

interface NodePosition {
  x: number;
  y: number;
}

function computeLayout(nodes: GraphNode[], width: number, height: number): Record<string, NodePosition> {
  const cx = width / 2;
  const cy = height / 2;

  // Sort by centrality descending so high-centrality nodes end up first
  const sorted = [...nodes].sort((a, b) => b.centrality - a.centrality);

  // The most central node goes in the center; rest arranged in a ring
  const positions: Record<string, NodePosition> = {};

  if (sorted.length === 0) return positions;

  // First node in center
  positions[sorted[0].id] = { x: cx, y: cy };

  if (sorted.length === 1) return positions;

  const radius = Math.min(width, height) * 0.32;
  const rest = sorted.slice(1);
  rest.forEach((node, i) => {
    const angle = (2 * Math.PI * i) / rest.length - Math.PI / 2;
    positions[node.id] = {
      x: cx + radius * Math.cos(angle),
      y: cy + radius * Math.sin(angle),
    };
  });

  return positions;
}

function nodeRadius(centrality: number): number {
  return 18 + centrality * 20; // 18–38px
}

function truncate(text: string, max: number): string {
  return text.length > max ? text.slice(0, max - 1) + '…' : text;
}

// Arrow marker for edges
function edgeKey(e: GraphEdge): string {
  return `${e.source}-${e.target}-${e.relation_type}`;
}

// Compute the point on the node circle boundary that faces the other node
function boundaryPoint(from: NodePosition, to: NodePosition, radius: number): NodePosition {
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const dist = Math.sqrt(dx * dx + dy * dy) || 1;
  return { x: from.x + (dx / dist) * radius, y: from.y + (dy / dist) * radius };
}

// --- GraphPanel component ---

interface GraphPanelProps {
  node: GraphNode;
  edges: GraphEdge[];
  nodes: GraphNode[];
  onClose: () => void;
  onSelectNode: (id: string) => void;
}

function GraphPanel({ node, edges, nodes, onClose, onSelectNode }: GraphPanelProps) {
  const connected = edges.filter(e => e.source === node.id || e.target === node.id);
  const nodeMap = Object.fromEntries(nodes.map(n => [n.id, n]));

  return (
    <div className="w-72 flex-shrink-0 bg-white border-l border-gray-200 flex flex-col overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 border-b border-gray-200 flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0">
          <h3 className="font-semibold text-[#1d1c1d] text-[14px] leading-snug">{node.title}</h3>
          <div className="flex items-center gap-1.5 mt-1">
            <span
              className="inline-block w-2 h-2 rounded-full flex-shrink-0"
              style={{ background: SIGNAL_COLORS[node.signal_level] }}
            />
            <span className="text-[11px] text-gray-500 capitalize">{node.signal_level} signal</span>
            <span className="text-gray-300 text-[11px]">·</span>
            <span className="text-[11px] text-gray-500 capitalize">{(node.event_type ?? '').replace('_', ' ')}</span>
          </div>
        </div>
        <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-lg leading-none mt-0.5">×</button>
      </div>

      {/* Stats */}
      <div className="px-4 py-3 border-b border-gray-100 grid grid-cols-3 gap-2 text-center">
        <div>
          <p className="text-[11px] text-gray-400">Centrality</p>
          <p className="text-[15px] font-semibold text-[#1d1c1d]">{node.centrality.toFixed(2)}</p>
        </div>
        <div>
          <p className="text-[11px] text-gray-400">Upstream</p>
          <p className="text-[15px] font-semibold text-[#1d1c1d]">{node.upstream_count}</p>
        </div>
        <div>
          <p className="text-[11px] text-gray-400">Downstream</p>
          <p className="text-[15px] font-semibold text-[#1d1c1d]">{node.downstream_count}</p>
        </div>
      </div>

      {/* Cluster */}
      {node.issue_cluster_id && (
        <div className="px-4 py-2 border-b border-gray-100">
          <span className="text-[11px] text-gray-400">Cluster: </span>
          <span className="text-[11px] text-gray-600 font-medium">{node.issue_cluster_id}</span>
        </div>
      )}

      {/* Connected edges */}
      <div className="flex-1 overflow-y-auto px-4 py-3">
        <p className="text-[11px] font-bold text-gray-400 uppercase tracking-wider mb-2">Connections</p>
        {connected.length === 0 && (
          <p className="text-[12px] text-gray-400 italic">No connections</p>
        )}
        {connected.map(edge => {
          const isSource = edge.source === node.id;
          const otherId = isSource ? edge.target : edge.source;
          const other = nodeMap[otherId];
          const style = EDGE_STYLES[edge.relation_type] || EDGE_STYLES['related_to'];
          return (
            <button
              key={edgeKey(edge)}
              onClick={() => other && onSelectNode(other.id)}
              className="w-full text-left mb-2 p-2 rounded border border-gray-100 hover:border-gray-300 hover:bg-gray-50 transition-colors"
            >
              <div className="flex items-center gap-1.5 mb-0.5">
                <span className="text-[10px] font-bold uppercase tracking-wider" style={{ color: style.stroke }}>
                  {isSource ? '→' : '←'} {edge.relation_type.replace('_', ' ')}
                </span>
                <span className="text-[10px] text-gray-400 ml-auto">{Math.round(edge.confidence * 100)}%</span>
              </div>
              <p className="text-[12px] text-[#1d1c1d] font-medium truncate">{other?.title || otherId}</p>
              <p className="text-[11px] text-gray-400 truncate">{edge.explanation}</p>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// --- Main GraphView ---

export default function GraphView() {
  const [graphData, setGraphData] = useState<GraphData>(MOCK_GRAPH_DATA);
  const [usingMock, setUsingMock] = useState(true);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [hoveredNodeId, setHoveredNodeId] = useState<string | null>(null);
  const [hoveredEdgeKey, setHoveredEdgeKey] = useState<string | null>(null);
  const [activeRelations, setActiveRelations] = useState<Set<string>>(new Set(ALL_RELATION_TYPES));
  const [tooltip, setTooltip] = useState<{ x: number; y: number; text: string } | null>(null);
  const [transform, setTransform] = useState({ x: 0, y: 0, scale: 1 });
  const [isPanning, setIsPanning] = useState(false);
  const panStart = useRef<{ mx: number; my: number; tx: number; ty: number } | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const SVG_W = 700;
  const SVG_H = 500;

  // Fetch graph data, fall back to mock.
  // Normalises backend field names (event_id, dominant_event_type, centrality_score,
  // source_event_id, target_event_id) to the frontend interface.
  useEffect(() => {
    fetchGraph()
      .then(data => {
        const nodes: GraphNode[] = (data?.nodes ?? []).map((n: Record<string, unknown>) => ({
          id: (n.id ?? n.event_id ?? '') as string,
          title: (n.title ?? '') as string,
          signal_level: ((n.signal_level ?? 'low') as GraphNode['signal_level']),
          event_type: (n.event_type ?? n.dominant_event_type ?? 'unknown') as string,
          centrality: (n.centrality ?? n.centrality_score ?? 0) as number,
          upstream_count: (n.upstream_count ?? 0) as number,
          downstream_count: (n.downstream_count ?? 0) as number,
          issue_cluster_id: (n.issue_cluster_id ?? null) as string | null,
        }));
        const edges: GraphEdge[] = (data?.edges ?? []).map((e: Record<string, unknown>) => ({
          source: (e.source ?? e.source_event_id ?? '') as string,
          target: (e.target ?? e.target_event_id ?? '') as string,
          relation_type: (e.relation_type ?? 'related_to') as GraphEdge['relation_type'],
          confidence: (e.confidence ?? 0.5) as number,
          explanation: (e.explanation ?? '') as string,
        }));
        if (nodes.length > 0) {
          setGraphData({ nodes, edges });
          setUsingMock(false);
        }
        // else: keep mock data, usingMock stays true
      })
      .catch(() => { /* keep mock data */ });
  }, []);

  const positions = computeLayout(graphData.nodes, SVG_W, SVG_H);
  const visibleEdges = graphData.edges.filter(e => activeRelations.has(e.relation_type));

  const selectedNode = graphData.nodes.find(n => n.id === selectedNodeId) || null;

  // Set of edge keys connected to hovered/selected node
  const highlightEdgeKeys = new Set<string>();
  const focusNodeId = hoveredNodeId || selectedNodeId;
  if (focusNodeId) {
    visibleEdges.forEach(e => {
      if (e.source === focusNodeId || e.target === focusNodeId) {
        highlightEdgeKeys.add(edgeKey(e));
      }
    });
  }

  // Pan handlers
  const onMouseDown = useCallback((e: React.MouseEvent<SVGSVGElement>) => {
    if ((e.target as Element).closest('.graph-node')) return;
    setIsPanning(true);
    panStart.current = { mx: e.clientX, my: e.clientY, tx: transform.x, ty: transform.y };
  }, [transform]);

  const onMouseMove = useCallback((e: React.MouseEvent<SVGSVGElement>) => {
    if (!isPanning || !panStart.current) return;
    const dx = e.clientX - panStart.current.mx;
    const dy = e.clientY - panStart.current.my;
    setTransform(t => ({ ...t, x: panStart.current!.tx + dx, y: panStart.current!.ty + dy }));
  }, [isPanning]);

  const onMouseUp = useCallback(() => {
    setIsPanning(false);
    panStart.current = null;
  }, []);

  const onWheel = useCallback((e: React.WheelEvent<SVGSVGElement>) => {
    e.preventDefault();
    const delta = e.deltaY > 0 ? 0.9 : 1.1;
    setTransform(t => ({ ...t, scale: Math.max(0.3, Math.min(3, t.scale * delta)) }));
  }, []);

  const resetView = () => setTransform({ x: 0, y: 0, scale: 1 });

  const toggleRelation = (rel: string) => {
    setActiveRelations(prev => {
      const next = new Set(prev);
      if (next.has(rel)) next.delete(rel);
      else next.add(rel);
      return next;
    });
  };

  return (
    <div className="flex flex-col h-full bg-white">
      {/* Header */}
      <div className="px-4 py-2.5 border-b border-gray-200 flex-shrink-0 flex items-center justify-between gap-4">
        <div className="flex items-center gap-2">
          <span className="text-[18px] leading-none">⬡</span>
          <div>
            <h2 className="font-bold text-[#1d1c1d] text-[15px] leading-tight">Event Graph</h2>
            <p className="text-[12px] text-gray-500 leading-tight">
              {graphData.nodes.length} events · {graphData.edges.length} relationships
              {usingMock && <span className="ml-2 text-amber-500">using mock data</span>}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-3">
          {/* Relation type filters */}
          <div className="flex items-center gap-2 flex-wrap">
            {ALL_RELATION_TYPES.map(rel => {
              const style = EDGE_STYLES[rel];
              const active = activeRelations.has(rel);
              return (
                <button
                  key={rel}
                  onClick={() => toggleRelation(rel)}
                  className={`flex items-center gap-1 px-2 py-0.5 rounded text-[11px] border transition-colors ${
                    active ? 'border-gray-300 bg-white' : 'border-gray-100 bg-gray-50 opacity-40'
                  }`}
                >
                  <span
                    className="inline-block w-4 h-px"
                    style={{
                      background: style.stroke,
                      borderTop: `2px ${style.dashArray === 'none' ? 'solid' : 'dashed'} ${style.stroke}`,
                    }}
                  />
                  <span className="capitalize" style={{ color: active ? style.stroke : '#97A0AF' }}>
                    {rel.replace('_', ' ')}
                  </span>
                </button>
              );
            })}
          </div>

          <button
            onClick={resetView}
            className="text-[12px] text-gray-500 border border-gray-200 px-2 py-0.5 rounded hover:bg-gray-50 transition-colors"
          >
            Reset view
          </button>
        </div>
      </div>

      {/* Graph + Panel */}
      <div className="flex flex-1 overflow-hidden">
        {/* SVG canvas */}
        <div className="flex-1 relative overflow-hidden bg-[#fafafa]">
          <svg
            ref={svgRef}
            width="100%"
            height="100%"
            viewBox={`0 0 ${SVG_W} ${SVG_H}`}
            preserveAspectRatio="xMidYMid meet"
            className={isPanning ? 'cursor-grabbing' : 'cursor-grab'}
            onMouseDown={onMouseDown}
            onMouseMove={onMouseMove}
            onMouseUp={onMouseUp}
            onMouseLeave={onMouseUp}
            onWheel={onWheel}
          >
            <defs>
              {ALL_RELATION_TYPES.map(rel => {
                const style = EDGE_STYLES[rel];
                return (
                  <marker
                    key={rel}
                    id={`arrow-${rel}`}
                    markerWidth="8"
                    markerHeight="8"
                    refX="6"
                    refY="3"
                    orient="auto"
                  >
                    <path d="M0,0 L0,6 L8,3 z" fill={style.stroke} />
                  </marker>
                );
              })}
            </defs>

            <g transform={`translate(${transform.x}, ${transform.y}) scale(${transform.scale})`}>
              {/* Edges */}
              {visibleEdges.map(edge => {
                const srcPos = positions[edge.source];
                const tgtPos = positions[edge.target];
                if (!srcPos || !tgtPos) return null;
                const style = EDGE_STYLES[edge.relation_type];
                const key = edgeKey(edge);
                const isHighlighted = highlightEdgeKeys.has(key);
                const dimmed = focusNodeId ? !isHighlighted : false;

                const srcR = nodeRadius(graphData.nodes.find(n => n.id === edge.source)?.centrality ?? 0.5);
                const tgtR = nodeRadius(graphData.nodes.find(n => n.id === edge.target)?.centrality ?? 0.5);
                const from = boundaryPoint(srcPos, tgtPos, srcR + 2);
                const to = boundaryPoint(tgtPos, srcPos, tgtR + 8);

                // Slight curve via a quadratic bezier control point
                const mx = (from.x + to.x) / 2 - (to.y - from.y) * 0.15;
                const my = (from.y + to.y) / 2 + (to.x - from.x) * 0.15;

                return (
                  <path
                    key={key}
                    d={`M ${from.x} ${from.y} Q ${mx} ${my} ${to.x} ${to.y}`}
                    stroke={style.stroke}
                    strokeWidth={isHighlighted ? 2.5 : 1.5}
                    strokeDasharray={style.dashArray === 'none' ? undefined : style.dashArray}
                    strokeOpacity={dimmed ? 0.1 : edge.confidence}
                    fill="none"
                    markerEnd={`url(#arrow-${edge.relation_type})`}
                    className="transition-all duration-150 cursor-pointer"
                    onMouseEnter={ev => {
                      setHoveredEdgeKey(key);
                      const rect = svgRef.current?.getBoundingClientRect();
                      if (rect) {
                        setTooltip({
                          x: ev.clientX - rect.left,
                          y: ev.clientY - rect.top,
                          text: `${edge.relation_type.replace('_', ' ')} · ${Math.round(edge.confidence * 100)}% · ${edge.explanation}`,
                        });
                      }
                    }}
                    onMouseLeave={() => {
                      setHoveredEdgeKey(null);
                      setTooltip(null);
                    }}
                  />
                );
              })}

              {/* Nodes */}
              {graphData.nodes.map(node => {
                const pos = positions[node.id];
                if (!pos) return null;
                const r = nodeRadius(node.centrality);
                const color = SIGNAL_COLORS[node.signal_level];
                const isSelected = node.id === selectedNodeId;
                const isHovered = node.id === hoveredNodeId;
                const dimmed = focusNodeId ? (node.id !== focusNodeId && !highlightEdgeKeys.size) : false;
                const connectedToFocus = focusNodeId
                  ? visibleEdges.some(e => (e.source === focusNodeId && e.target === node.id) || (e.target === focusNodeId && e.source === node.id))
                  : false;
                const shouldDim = focusNodeId
                  ? (node.id !== focusNodeId && !connectedToFocus)
                  : false;

                return (
                  <g
                    key={node.id}
                    className="graph-node"
                    transform={`translate(${pos.x}, ${pos.y})`}
                    style={{ cursor: 'pointer' }}
                    onClick={() => setSelectedNodeId(node.id === selectedNodeId ? null : node.id)}
                    onMouseEnter={ev => {
                      setHoveredNodeId(node.id);
                      const rect = svgRef.current?.getBoundingClientRect();
                      if (rect) {
                        setTooltip({
                          x: ev.clientX - rect.left,
                          y: ev.clientY - rect.top,
                          text: node.title,
                        });
                      }
                    }}
                    onMouseLeave={() => {
                      setHoveredNodeId(null);
                      setTooltip(null);
                    }}
                    opacity={shouldDim ? 0.2 : 1}
                  >
                    {/* Selection ring */}
                    {isSelected && (
                      <circle r={r + 5} fill="none" stroke={color} strokeWidth={2} strokeOpacity={0.4} />
                    )}
                    {/* Node circle */}
                    <circle
                      r={r}
                      fill={color}
                      fillOpacity={isHovered || isSelected ? 1 : 0.85}
                      stroke={isSelected ? color : '#fff'}
                      strokeWidth={isSelected ? 3 : 1.5}
                      className="transition-all duration-100"
                    />
                    {/* Node label */}
                    <text
                      textAnchor="middle"
                      dominantBaseline="middle"
                      fontSize={10}
                      fill="#fff"
                      fontWeight="600"
                      style={{ pointerEvents: 'none', userSelect: 'none' }}
                    >
                      {truncate(node.title, 14)}
                    </text>
                    {/* Event type badge below */}
                    <text
                      y={r + 12}
                      textAnchor="middle"
                      fontSize={9}
                      fill="#555"
                      style={{ pointerEvents: 'none', userSelect: 'none' }}
                    >
                      {(node.event_type ?? '').replace('_', ' ')}
                    </text>
                  </g>
                );
              })}
            </g>
          </svg>

          {/* Tooltip */}
          {tooltip && (
            <div
              className="absolute pointer-events-none bg-[#1d1c1d] text-white text-[11px] px-2 py-1 rounded shadow-lg max-w-[220px] z-10"
              style={{ left: tooltip.x + 12, top: tooltip.y - 8 }}
            >
              {tooltip.text}
            </div>
          )}

          {/* Legend */}
          <div className="absolute bottom-3 left-3 bg-white border border-gray-200 rounded shadow-sm px-3 py-2 text-[11px] text-gray-600 space-y-1">
            <p className="font-semibold text-gray-700 mb-1">Signal level</p>
            {(['high', 'medium', 'low'] as const).map(level => (
              <div key={level} className="flex items-center gap-1.5">
                <span className="w-2.5 h-2.5 rounded-full inline-block" style={{ background: SIGNAL_COLORS[level] }} />
                <span className="capitalize">{level}</span>
              </div>
            ))}
            <p className="text-gray-400 mt-1 pt-1 border-t border-gray-100">Scroll to zoom · Drag to pan</p>
          </div>
        </div>

        {/* Detail panel */}
        {selectedNode && (
          <GraphPanel
            node={selectedNode}
            edges={graphData.edges}
            nodes={graphData.nodes}
            onClose={() => setSelectedNodeId(null)}
            onSelectNode={id => setSelectedNodeId(id)}
          />
        )}
      </div>
    </div>
  );
}
