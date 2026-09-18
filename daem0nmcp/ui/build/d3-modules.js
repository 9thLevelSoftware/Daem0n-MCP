// D3 modules for MCP Apps visualizations
// Selective imports to minimize bundle size

// DOM manipulation
export { select, selectAll, create } from 'd3-selection';

// Force-directed layout (graph viewer)
export {
  forceSimulation,
  forceLink,
  forceManyBody,
  forceCenter,
  forceCollide,
  forceX,
  forceY
} from 'd3-force';

// Scales and color schemes
export {
  scaleLinear,
  scaleOrdinal,
  scaleBand,
  scaleTime
} from 'd3-scale';
export { schemeCategory10, schemeTableau10 } from 'd3-scale-chromatic';

// Zoom/pan interaction
export { zoom, zoomIdentity, zoomTransform } from 'd3-zoom';

// Hierarchy (treemap for communities)
export {
  hierarchy,
  treemap,
  treemapBinary,
  treemapSquarify
} from 'd3-hierarchy';

// Shapes (arcs for pie charts, paths)
export { arc, pie, line, area } from 'd3-shape';

// Transitions and animation
export { transition } from 'd3-transition';
export { interpolate, interpolateRgb } from 'd3-interpolate';
export { easeCubicOut, easeElasticOut, easeQuadOut } from 'd3-ease';
