#!/usr/bin/env python3
"""Metamodel Visualizer — generates an interactive HTML graph from a metamodel JSON."""

import argparse
import json
import sys
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description="Visualize a metamodel JSON as an interactive HTML graph.")
    parser.add_argument("input", help="Path to metamodel JSON file")
    parser.add_argument("-o", "--output", default="metamodel.html", help="Output HTML file (default: metamodel.html)")
    return parser.parse_args()


def load_metamodel(path):
    with open(path) as f:
        data = json.load(f)
    if "elements" not in data or "relationships" not in data:
        print("Error: metamodel JSON must have 'elements' and 'relationships' keys.", file=sys.stderr)
        sys.exit(1)
    return data


def validate_metamodel(data):
    """Validate all references point to existing elements."""
    element_names = set(data["elements"].keys())
    errors = []

    for name, el in data["elements"].items():
        if "extend" in el and el["extend"] not in element_names:
            errors.append(f"element '{name}' extends '{el['extend']}', which does not exist.")
        for ref in el.get("is_owned_by_one_of", []):
            if ref not in element_names:
                errors.append(f"element '{name}' is_owned_by '{ref}', which does not exist.")
        for ref in el.get("is_typed_by_one_of", []):
            if ref not in element_names:
                errors.append(f"element '{name}' is_typed_by '{ref}', which does not exist.")

    for rel_name, rel in data["relationships"].items():
        for mapping in rel.get("mappings", []):
            src = mapping.get("source")
            dst = mapping.get("destination")
            if src not in element_names:
                errors.append(f"relationship '{rel_name}' references source '{src}', which does not exist.")
            if dst not in element_names:
                errors.append(f"relationship '{rel_name}' references destination '{dst}', which does not exist.")

    if errors:
        for e in errors:
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def resolve_inheritance(elements):
    """Resolve extend chains via topological sort. Returns enriched elements with inherited info."""

    # Build parent map
    parent_of = {}
    for name, el in elements.items():
        if "extend" in el:
            parent_of[name] = el["extend"]

    # Topological sort (Kahn's algorithm)
    children_of = {}
    in_degree = {name: 0 for name in elements}
    for child, parent in parent_of.items():
        children_of.setdefault(parent, []).append(child)
        in_degree[child] = in_degree.get(child, 0) + 1

    queue = [name for name, deg in in_degree.items() if deg == 0]
    order = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        for child in children_of.get(node, []):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if len(order) != len(elements):
        cycle_members = [n for n in elements if n not in order]
        print(f"Error: circular inheritance detected among: {', '.join(cycle_members)}", file=sys.stderr)
        sys.exit(1)

    # Resolve in topo order
    resolved = {}
    for name in order:
        el = elements[name]
        own = {
            "id_properties": list(el.get("id_properties", [])),
            "properties": list(el.get("properties", [])),
            "optional_properties": list(el.get("optional_properties", [])),
            "is_owned_by_one_of": list(el.get("is_owned_by_one_of", [])),
            "is_typed_by_one_of": list(el.get("is_typed_by_one_of", [])),
        }

        inherited = {
            "id_properties": [],
            "properties": [],
            "optional_properties": [],
            "is_owned_by_one_of": [],
            "is_typed_by_one_of": [],
        }

        if name in parent_of:
            parent_resolved = resolved[parent_of[name]]
            # Inherit = parent's own + parent's inherited, minus what child already declares
            for key in inherited:
                parent_all = parent_resolved["inherited"][key] + parent_resolved["own"][key]
                own_set = set(own[key])
                inherited[key] = [v for v in parent_all if v not in own_set]

        resolved[name] = {"own": own, "inherited": inherited, "extend": el.get("extend")}

    return resolved, parent_of


def resolve_relationship_inheritance(relationships, elements, parent_of):
    """Clone relationship mappings for child elements that inherit from parents."""
    child_of = {}
    for child, parent in parent_of.items():
        child_of.setdefault(parent, []).append(child)

    # Collect all descendants for each element
    def get_descendants(name):
        desc = []
        for child in child_of.get(name, []):
            desc.append(child)
            desc.extend(get_descendants(child))
        return desc

    new_relationships = {}
    for rel_name, rel in relationships.items():
        new_mappings = list(rel["mappings"])
        for mapping in rel["mappings"]:
            src_descendants = get_descendants(mapping["source"])
            dst_descendants = get_descendants(mapping["destination"])
            for desc in src_descendants:
                new_mappings.append({"source": desc, "destination": mapping["destination"], "inherited": True, "inherited_side": "source"})
            for desc in dst_descendants:
                new_mappings.append({"source": mapping["source"], "destination": desc, "inherited": True, "inherited_side": "destination"})
            for sd in src_descendants:
                for dd in dst_descendants:
                    new_mappings.append({"source": sd, "destination": dd, "inherited": True, "inherited_side": "both"})
        new_relationships[rel_name] = {"properties": rel["properties"], "mappings": new_mappings}

    return new_relationships


def build_graph_data(resolved, relationships, parent_of):
    """Build Cytoscape.js elements array."""
    nodes = []
    edges = []
    edge_id = 0

    # Color palette for relationships
    palette = ["#ef476f", "#ffd166", "#06d6a0", "#118ab2", "#f78c6b", "#b07cc6", "#26c6da", "#ff6b6b", "#78e08f", "#e056a0"]

    # Element nodes
    for name, data in resolved.items():
        own = data["own"]
        inh = data["inherited"]
        node_data = {
            "id": name,
            "label": name,
            "type": "element",
            "id_properties": own["id_properties"],
            "properties": own["properties"],
            "optional_properties": own["optional_properties"],
            "owners": own["is_owned_by_one_of"] + inh["is_owned_by_one_of"],
            "typed_by": own["is_typed_by_one_of"] + inh["is_typed_by_one_of"],
            "extends": data.get("extend"),
            "inherited_id_properties": inh["id_properties"],
            "inherited_properties": inh["properties"],
            "inherited_optional_properties": inh["optional_properties"],
        }
        nodes.append({"data": node_data})

    # Extends edges
    for child_name, parent_name in parent_of.items():
        edges.append({"data": {"id": f"e{edge_id}", "source": child_name, "target": parent_name, "label": "extends", "edgeType": "extends"}})
        edge_id += 1

    # Typing edges (to other elements)
    for name, data in resolved.items():
        all_types = data["own"]["is_typed_by_one_of"] + data["inherited"]["is_typed_by_one_of"]
        for t in all_types:
            edges.append({"data": {"id": f"e{edge_id}", "source": t, "target": name, "label": "types", "edgeType": "typing"}})
            edge_id += 1

    # Ownership edges
    for name, data in resolved.items():
        all_owners = data["own"]["is_owned_by_one_of"] + data["inherited"]["is_owned_by_one_of"]
        for owner in all_owners:
            edges.append({"data": {"id": f"e{edge_id}", "source": owner, "target": name, "label": "owns", "edgeType": "ownership"}})
            edge_id += 1

    # Relationship edges
    rel_colors = {}
    for i, rel_name in enumerate(relationships):
        rel_colors[rel_name] = palette[i % len(palette)]

    for rel_name, rel in relationships.items():
        props = rel.get("properties", [])
        for mapping in rel["mappings"]:
            label = rel_name
            if props:
                label += f" [{', '.join(props)}]"
            is_inherited = mapping.get("inherited", False)
            edge_data = {
                "id": f"e{edge_id}",
                "source": mapping["source"],
                "target": mapping["destination"],
                "label": label,
                "edgeType": "relationship",
                "relName": rel_name,
                "relColor": rel_colors[rel_name],
            }
            if is_inherited:
                edge_data["inherited"] = True
                edge_data["inheritedSide"] = mapping.get("inherited_side", "both")
            edges.append({"data": edge_data})
            edge_id += 1

    return nodes + edges, rel_colors


def build_node_label(data):
    """Build multi-line label for element nodes."""
    lines = [data["label"], "─" * max(len(data["label"]), 12)]

    for p in data.get("id_properties", []):
        lines.append(f"🔑 {p}")
    for p in data.get("properties", []):
        lines.append(f"📌 {p}")
    for p in data.get("optional_properties", []):
        lines.append(f"❓  {p}")

    return "\n".join(lines)


def generate_html(cy_elements, rel_colors):
    """Generate the self-contained HTML file."""

    # Pre-process: build labels for element nodes
    for el in cy_elements:
        if "data" in el and el["data"].get("type") == "element":
            el["data"]["_label"] = build_node_label(el["data"])

    elements_json = json.dumps(cy_elements, indent=2)

    rel_legend_items = ""
    for rel_name, color in rel_colors.items():
        rel_legend_items += f'  <div><label class="legend-toggle"><input type="checkbox" checked onchange="toggleEdgeClass(\'relationship\', \'{rel_name}\', this.checked)" data-edge-rel="{rel_name}"><span class="swatch" style="background:{color}"></span> {rel_name}</label></div>\n'

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Metamodel Visualization</title>
<script src="https://unpkg.com/cytoscape@3.28.1/dist/cytoscape.min.js"></script>
<script src="https://unpkg.com/dagre@0.8.5/dist/dagre.min.js"></script>
<script src="https://unpkg.com/cytoscape-dagre@2.5.0/cytoscape-dagre.js"></script>
<style>
  :root {{
    --bg: #0d1117; --bg-surface: #161b22; --bg-elevated: #1c2333;
    --bg-btn: #21262d; --bg-btn-hover: #30363d; --bg-btn-active: #30363d;
    --border: #30363d; --border-active: #8b949e;
    --text: #e6edf3; --text-muted: #8b949e; --text-dimmed: #6e7681;
    --accent: #e94560; --accent-key: #f0883e;
    --node-bg: #1a1a2e; --node-border: #e94560;
    --type-bg: #2a2a3e; --type-border: #533483; --type-text: #a8a8b8;
    --label-bg: #0d1117;
    --edge-ownership: #5b9bd5; --edge-typing: #a07cc6; --edge-extends: #5bb88a;
  }}
  body.light {{
    --bg: #f6f8fa; --bg-surface: #ffffff; --bg-elevated: #ffffff;
    --bg-btn: #f0f0f0; --bg-btn-hover: #e0e0e0; --bg-btn-active: #d8d8d8;
    --border: #d0d7de; --border-active: #656d76;
    --text: #1f2328; --text-muted: #656d76; --text-dimmed: #8b949e;
    --accent: #cf222e; --accent-key: #bc4c00;
    --node-bg: #f0f4ff; --node-border: #cf222e;
    --type-bg: #f5f0ff; --type-border: #8250df; --type-text: #6e5494;
    --label-bg: #f6f8fa;
    --edge-ownership: #2c5d8f; --edge-typing: #7c4daf; --edge-extends: #2d7a50;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  html, body {{ height: 100%; }}
  body {{ background: var(--bg); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; overflow: hidden; transition: background 0.3s; display: flex; flex-direction: column; }}
  #banner {{
    background: var(--bg-surface); color: var(--text); padding: 8px 16px;
    border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
    transition: background 0.3s, color 0.3s, border-color 0.3s;
    position: relative; z-index: 10; flex-shrink: 0;
  }}
  #banner h1 {{ font-size: 16px; font-weight: 600; white-space: nowrap; }}
  #banner .controls {{ margin-left: auto; display: flex; align-items: center; gap: 8px; }}
  .theme-toggle {{
    background: none; border: none; cursor: pointer; font-size: 18px;
    padding: 4px 8px; border-radius: 6px; transition: background 0.15s;
    line-height: 1; color: var(--text-muted);
  }}
  .theme-toggle:hover {{ background: var(--bg-btn-hover); color: var(--text); }}
  #banner .hint {{ color: var(--text-muted); font-size: 13px; }}
  .settings-wrap {{ position: relative; }}
  .settings-btn {{
    background: none; border: none; cursor: pointer; font-size: 18px;
    padding: 4px 8px; border-radius: 6px; transition: background 0.15s;
    line-height: 1; color: var(--text-muted);
  }}
  .settings-btn:hover {{ background: var(--bg-btn-hover); color: var(--text); }}
  .settings-panel {{
    display: none; position: fixed; top: auto; right: 12px;
    background: var(--bg-elevated); border: 1px solid var(--border);
    border-radius: 10px; padding: 16px 18px; width: max-content;
    box-shadow: 0 8px 24px rgba(0,0,0,0.3); z-index: 200;
    transition: background 0.3s, border-color 0.3s;
  }}
  .settings-panel.open {{ display: block; }}
  .settings-panel .sp-title {{ font-size: 12px; font-weight: 600; color: var(--text); margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.5px; }}
  .settings-panel .sp-row {{
    display: flex; align-items: center; justify-content: space-between;
    padding: 6px 0; font-size: 13px; color: var(--text); gap: 20px; white-space: nowrap;
  }}
  .settings-panel .sp-row + .sp-row {{ border-top: 1px solid var(--border); }}
  /* Toggle switch */
  .switch {{ position: relative; width: 36px; height: 20px; flex-shrink: 0; }}
  .switch input {{ opacity: 0; width: 0; height: 0; }}
  .switch .slider {{
    position: absolute; inset: 0; background: var(--bg-btn); border-radius: 20px;
    cursor: pointer; transition: background 0.2s;
  }}
  .switch .slider::before {{
    content: ''; position: absolute; width: 14px; height: 14px;
    left: 3px; bottom: 3px; background: var(--text-muted); border-radius: 50%;
    transition: transform 0.2s, background 0.2s;
  }}
  .switch input:checked + .slider {{ background: var(--accent); }}
  /* Range slider */
  .sp-slider {{ display: flex; flex-direction: column; gap: 10px; width: 100%; }}
  .sp-slider input[type="range"] {{
    -webkit-appearance: none; width: 100%; height: 4px; border-radius: 2px;
    background: var(--bg-btn); outline: none; cursor: pointer;
  }}
  .sp-slider input[type="range"]::-webkit-slider-thumb {{
    -webkit-appearance: none; width: 14px; height: 14px; border-radius: 50%;
    background: var(--accent); cursor: pointer;
  }}
  .sp-slider input[type="range"]::-moz-range-thumb {{
    width: 14px; height: 14px; border-radius: 50%; border: none;
    background: var(--accent); cursor: pointer;
  }}
  .switch input:checked + .slider::before {{ transform: translateX(16px); background: white; }}
  #cy {{ width: 100%; flex: 1; min-height: 0; }}
  #tooltip {{
    position: fixed; background: var(--bg-elevated); color: var(--text);
    padding: 14px 18px; border-radius: 8px; font-size: 13px;
    pointer-events: none; display: none; max-width: 360px;
    border: 1px solid var(--border); box-shadow: 0 4px 16px rgba(0,0,0,0.2);
    line-height: 1.6; z-index: 100;
    transition: background 0.3s, color 0.3s, border-color 0.3s;
  }}
  #tooltip .tt-name {{ color: var(--accent); font-size: 15px; font-weight: 600; }}
  #tooltip .tt-section {{ margin-top: 8px; }}
  #tooltip .tt-section-title {{ color: var(--text-muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 2px; }}
  #tooltip .tt-inherited {{ color: var(--text-dimmed); font-style: italic; }}
  #tooltip .tt-key {{ color: var(--accent-key); }}
  #tooltip .tt-opt {{ color: var(--text-muted); }}
  #tooltip .tt-dir {{ color: #26c6da; font-style: italic; }}
  #tooltip .tt-icon {{ display: inline-block; width: 22px; text-align: center; }}
  #legend {{
    position: fixed; bottom: 16px; left: 16px;
    background: var(--bg-surface); border: 1px solid var(--border); border-radius: 8px;
    padding: 14px 18px; color: var(--text); font-size: 12px; z-index: 50;
    line-height: 1.8;
    transition: background 0.3s, color 0.3s, border-color 0.3s;
  }}
  #legend .title {{ font-weight: 600; margin-bottom: 4px; }}
  #legend div {{ display: flex; align-items: center; gap: 8px; }}
  #legend .swatch {{ width: 28px; height: 3px; border-radius: 2px; display: inline-block; flex-shrink: 0; }}
  /* Search */
  .search-wrap {{ position: relative; display: flex; align-items: center; }}
  .search-input {{
    background: var(--bg-btn); color: var(--text); border: 1px solid var(--border);
    padding: 5px 28px 5px 10px; border-radius: 6px; font-size: 13px;
    outline: none; width: 180px; transition: border-color 0.15s, width 0.2s;
  }}
  .search-input:focus {{ border-color: var(--accent); width: 220px; }}
  .search-input::placeholder {{ color: var(--text-dimmed); }}
  .search-clear {{
    position: absolute; right: 6px; background: none; border: none;
    color: var(--text-dimmed); cursor: pointer; font-size: 14px; padding: 2px;
    display: none; line-height: 1;
  }}
  .search-clear:hover {{ color: var(--text); }}
  .search-filters {{
    position: fixed; right: 12px;
    background: var(--bg-elevated); border: 1px solid var(--border);
    border-radius: 8px; padding: 10px 14px; width: max-content;
    box-shadow: 0 8px 24px rgba(0,0,0,0.3); z-index: 200;
    display: none; transition: background 0.3s, border-color 0.3s;
  }}
  .search-filters.open {{ display: block; }}
  .search-filters label {{
    display: flex; align-items: center; gap: 6px;
    font-size: 12px; color: var(--text); padding: 3px 0; cursor: pointer; white-space: nowrap;
  }}
  .search-filters input {{ accent-color: var(--accent); cursor: pointer; }}
  .search-filter-btn {{
    background: none; border: none; cursor: pointer; font-size: 14px;
    color: var(--text-muted); padding: 4px; line-height: 1; border-radius: 4px;
    transition: background 0.15s;
  }}
  .search-filter-btn:hover {{ background: var(--bg-btn-hover); color: var(--text); }}
  .search-counter {{
    font-size: 11px; color: var(--text-dimmed); margin-left: 4px; white-space: nowrap;
  }}
  #legend .legend-btns {{ display: flex; gap: 4px; }}
  #legend .legend-btns button {{
    background: var(--bg-btn); color: var(--text-muted); border: 1px solid var(--border);
    padding: 2px 8px; border-radius: 4px; font-size: 10px; cursor: pointer;
    transition: background 0.15s;
  }}
  #legend .legend-btns button:hover {{ background: var(--bg-btn-hover); color: var(--text); }}
  #legend .legend-toggle {{
    display: flex; align-items: center; gap: 6px; cursor: pointer; white-space: nowrap;
  }}
  #legend .legend-toggle input {{ accent-color: var(--accent); cursor: pointer; margin: 0; }}
  #legend .swatch-dashed {{ background: repeating-linear-gradient(90deg, var(--edge-ownership) 0, var(--edge-ownership) 6px, transparent 6px, transparent 10px); }}
  #legend .swatch-dotted {{ background: repeating-linear-gradient(90deg, var(--edge-typing) 0, var(--edge-typing) 3px, transparent 3px, transparent 7px); }}
</style>
</head>
<body>
<div id="banner">
  <h1>Metamodel Visualization</h1>
  <span class="controls">
    <div class="search-wrap">
      <input class="search-input" id="searchInput" type="text" placeholder="Search..." oninput="onSearch(this.value)" onkeydown="onSearchKey(event)">
      <button class="search-clear" id="searchClear" onclick="clearSearch()">&#10005;</button>
      <span class="search-counter" id="searchCounter"></span>
      <button class="search-filter-btn" onclick="toggleSearchFilters(event)" title="Search filters">&#9662;</button>
      <div class="search-filters" id="searchFilters">
        <label><input type="checkbox" checked class="search-scope" value="elements"> Element names</label>
        <label><input type="checkbox" checked class="search-scope" value="properties"> Properties</label>
        <label><input type="checkbox" checked class="search-scope" value="relationships"> Relationships</label>
        <label><input type="checkbox" checked class="search-scope" value="types"> Typed by</label>
      </div>
    </div>
    <button class="theme-toggle" onclick="toggleTheme()" title="Toggle light/dark mode" id="themeBtn">&#9789;</button>
    <div class="settings-wrap">
      <button class="settings-btn" onclick="toggleSettings(event)" title="Settings">&#9881;</button>
      <div class="settings-panel" id="settingsPanel">
        <div class="sp-title">Settings</div>
        <div class="sp-row">
          <span>Inherited relationships</span>
          <label class="switch"><input type="checkbox" id="toggleInherited" onchange="toggleInheritedEdges(this.checked)"><span class="slider"></span></label>
        </div>
        <div class="sp-row">
          <span>Show legend</span>
          <label class="switch"><input type="checkbox" id="toggleLegend" checked onchange="toggleLegend(this.checked)"><span class="slider"></span></label>
        </div>
        <div class="sp-row">
          <span>Dim others on hover</span>
          <label class="switch"><input type="checkbox" id="toggleDimMode"><span class="slider"></span></label>
        </div>
        <div class="sp-row">
          <span>Element details on hover</span>
          <label class="switch"><input type="checkbox" id="toggleElementHover" checked><span class="slider"></span></label>
        </div>
        <div class="sp-row">
          <span>Relationship details on hover</span>
          <label class="switch"><input type="checkbox" id="toggleRelHover" checked><span class="slider"></span></label>
        </div>
        <div class="sp-row">
          <div class="sp-slider">
            <span>Spacing</span>
            <input type="range" id="spacingSlider" min="0" max="100" value="25" oninput="applySpacing(this.value)">
          </div>
        </div>
      </div>
    </div>
  </span>
</div>
<div id="cy"></div>
<div id="tooltip"></div>
<div id="legend">
  <div class="title" style="display:flex;justify-content:space-between;align-items:center;">Edge Types <span class="legend-btns"><button onclick="setAllEdges(true)">All</button><button onclick="setAllEdges(false)">None</button></span></div>
{rel_legend_items}  <div><label class="legend-toggle"><input type="checkbox" checked onchange="toggleEdgeClass('ownership', null, this.checked)"><span class="swatch swatch-dashed"></span> Ownership</label></div>
  <div><label class="legend-toggle"><input type="checkbox" checked onchange="toggleEdgeClass('typing', null, this.checked)"><span class="swatch swatch-dotted"></span> Typing</label></div>
  <div><label class="legend-toggle"><input type="checkbox" checked onchange="toggleEdgeClass('extends', null, this.checked)"><span class="swatch" style="background:var(--edge-extends)"></span> Extends</label></div>
  <div class="title" style="display:flex;justify-content:space-between;align-items:center;margin-top:10px;">Properties <span class="legend-btns"><button onclick="setAllProps(true)">All</button><button onclick="setAllProps(false)">None</button></span></div>
  <div><label class="legend-toggle"><input type="checkbox" checked class="prop-toggle" value="id" onchange="togglePropType(this.value, this.checked)">🔑 ID (generates identifier)</label></div>
  <div><label class="legend-toggle"><input type="checkbox" checked class="prop-toggle" value="mandatory" onchange="togglePropType(this.value, this.checked)">📌 Mandatory</label></div>
  <div><label class="legend-toggle"><input type="checkbox" checked class="prop-toggle" value="optional" onchange="togglePropType(this.value, this.checked)">❓  Optional</label></div>
</div>

<script>
const elements = {elements_json};

// Relationship colors map
const relColors = {json.dumps(rel_colors)};

const cy = cytoscape({{
  container: document.getElementById('cy'),
  elements: elements,
  style: [
    // Element nodes
    {{
      selector: 'node[type="element"]',
      style: {{
        'shape': 'round-rectangle',
        'background-color': '#1a1a2e',
        'border-color': '#e94560',
        'border-width': 2,
        'label': 'data(_label)',
        'color': '#e6edf3',
        'text-valign': 'center',
        'text-halign': 'center',
        'font-size': '12px',
        'font-family': 'monospace',
        'width': 'label',
        'height': 'label',
        'padding': '20px',
        'text-wrap': 'wrap',
        'text-max-width': '300px',
        'text-justification': 'left',
      }}
    }},
    // Extends edges
    {{
      selector: 'edge[edgeType="extends"]',
      style: {{
        'width': 2,
        'line-color': '#5bb88a',
        'line-style': 'solid',
        'target-arrow-color': '#5bb88a',
        'target-arrow-shape': 'triangle',
        'curve-style': 'bezier',
        'label': 'extends',
        'font-size': '10px',
        'color': '#5bb88a',
        'text-rotation': 'autorotate',
        'text-margin-y': -10,
        'text-background-color': '#0d1117',
        'text-background-opacity': 0.85,
        'text-background-padding': '3px',
        'text-background-shape': 'roundrectangle',
      }}
    }},
    // Relationship edges (default — colored per-relationship via mappers below)
    {{
      selector: 'edge[edgeType="relationship"]',
      style: {{
        'width': 2.5,
        'line-color': 'data(relColor)',
        'target-arrow-color': 'data(relColor)',
        'target-arrow-shape': 'triangle',
        'curve-style': 'bezier',
        'label': 'data(label)',
        'font-size': '11px',
        'color': 'data(relColor)',
        'text-rotation': 'autorotate',
        'text-margin-y': -12,
        'text-background-color': '#0d1117',
        'text-background-opacity': 0.85,
        'text-background-padding': '3px',
        'text-background-shape': 'roundrectangle',
      }}
    }},
    // Inherited relationship edges
    {{
      selector: 'edge[edgeType="relationship"][inherited]',
      style: {{
        'line-style': 'dashed',
        'opacity': 0.6,
      }}
    }},
    // Ownership edges
    {{
      selector: 'edge[edgeType="ownership"]',
      style: {{
        'width': 1.5,
        'line-color': '#5b9bd5',
        'line-style': 'dashed',
        'target-arrow-color': '#5b9bd5',
        'target-arrow-shape': 'triangle',
        'curve-style': 'bezier',
        'label': 'owns',
        'font-size': '10px',
        'color': '#5b9bd5',
        'text-rotation': 'autorotate',
        'text-margin-y': -10,
        'text-background-color': '#0d1117',
        'text-background-opacity': 0.85,
        'text-background-padding': '3px',
        'text-background-shape': 'roundrectangle',
      }}
    }},
    // Typing edges
    {{
      selector: 'edge[edgeType="typing"]',
      style: {{
        'width': 1,
        'line-color': '#a07cc6',
        'line-style': 'dotted',
        'target-arrow-color': '#a07cc6',
        'target-arrow-shape': 'triangle',
        'curve-style': 'bezier',
        'label': 'types',
        'font-size': '10px',
        'color': '#a07cc6',
        'text-rotation': 'autorotate',
        'text-margin-y': -10,
        'text-background-color': '#0d1117',
        'text-background-opacity': 0.85,
        'text-background-padding': '3px',
        'text-background-shape': 'roundrectangle',
      }}
    }},
  ],
  layout: {{
    name: 'dagre',
    rankDir: 'TB',
    nodeSep: 30,
    rankSep: 50,
    edgeSep: 15,
    ranker: 'tight-tree',
  }},
  // Performance for large graphs
  textureOnViewport: true,
  hideEdgesOnViewport: false,
  wheelSensitivity: 0.15,
}});

// Theme toggle
const darkTheme = {{
  nodeBg: '#1a1a2e', nodeBorder: '#e94560', nodeText: '#e6edf3',
  parentBg: '#12121f',
  typeBg: '#2a2a3e', typeBorder: '#a07cc6', typeText: '#c4b0e0',
  labelBg: '#0d1117',
  ownership: '#5b9bd5', typing: '#a07cc6', extends: '#5bb88a',
}};
const lightTheme = {{
  nodeBg: '#f0f4ff', nodeBorder: '#cf222e', nodeText: '#1f2328',
  parentBg: '#e8ecf4',
  typeBg: '#f5f0ff', typeBorder: '#7c4daf', typeText: '#6e5494',
  labelBg: '#f6f8fa',
  ownership: '#2c5d8f', typing: '#7c4daf', extends: '#2d7a50',
}};
let isDark = true;

function applyThemeToGraph(theme) {{
  cy.nodes('[type="element"]').style({{
    'background-color': theme.nodeBg,
    'border-color': theme.nodeBorder,
    'color': theme.nodeText,
  }});
  cy.edges().style({{
    'text-background-color': theme.labelBg,
  }});
  cy.edges('[edgeType="ownership"]').style({{
    'line-color': theme.ownership,
    'target-arrow-color': theme.ownership,
    'color': theme.ownership,
  }});
  cy.edges('[edgeType="typing"]').style({{
    'line-color': theme.typing,
    'target-arrow-color': theme.typing,
    'color': theme.typing,
  }});
  cy.edges('[edgeType="extends"]').style({{
    'line-color': theme.extends,
    'target-arrow-color': theme.extends,
    'color': theme.extends,
  }});
}}

function toggleTheme() {{
  isDark = !isDark;
  document.body.classList.toggle('light', !isDark);
  document.getElementById('themeBtn').innerHTML = isDark ? '&#9789;' : '&#9788;';
  applyThemeToGraph(isDark ? darkTheme : lightTheme);
}}

// Property visibility
const propVisibility = {{ id: true, mandatory: true, optional: true }};

function togglePropType(type, show) {{
  propVisibility[type] = show;
  rebuildLabels();
}}

function setAllProps(show) {{
  document.querySelectorAll('.prop-toggle').forEach(cb => {{
    cb.checked = show;
    propVisibility[cb.value] = show;
  }});
  rebuildLabels();
}}

function rebuildLabels() {{
  cy.nodes('[type="element"]').forEach(node => {{
    const d = node.data();
    const lines = [d.label, '─'.repeat(Math.max(d.label.length, 12))];

    if (propVisibility.id) {{
      (d.id_properties || []).forEach(p => lines.push('🔑 ' + p));
    }}
    if (propVisibility.mandatory) {{
      (d.properties || []).forEach(p => lines.push('📌 ' + p));
    }}
    if (propVisibility.optional) {{
      (d.optional_properties || []).forEach(p => lines.push('❓  ' + p));
    }}

    node.data('_label', lines.join('\\n'));
  }});
}}

// Settings panel
function toggleSettings(e) {{
  e.stopPropagation();
  const panel = document.getElementById('settingsPanel');
  panel.classList.toggle('open');
  if (panel.classList.contains('open')) {{
    const btn = e.currentTarget.getBoundingClientRect();
    panel.style.top = (btn.bottom + 16) + 'px';
  }}
}}
document.addEventListener('click', function(e) {{
  const panel = document.getElementById('settingsPanel');
  if (panel.classList.contains('open') && !panel.contains(e.target)) {{
    panel.classList.remove('open');
  }}
}});

// Legend toggle
function toggleLegend(show) {{
  document.getElementById('legend').style.display = show ? 'block' : 'none';
}}

// Enable/disable all edges
function setAllEdges(show) {{
  document.querySelectorAll('#legend .legend-toggle input').forEach(cb => {{
    cb.checked = show;
    cb.dispatchEvent(new Event('change'));
  }});
}}

// Toggle edge class visibility
function toggleEdgeClass(edgeType, relName, show) {{
  let selector;
  if (edgeType === 'relationship' && relName) {{
    selector = `edge[edgeType="relationship"][relName="${{relName}}"]`;
  }} else {{
    selector = `edge[edgeType="${{edgeType}}"]`;
  }}
  cy.edges(selector).style('display', show ? 'element' : 'none');
}}

// Hide inherited relationship edges by default
cy.edges('[edgeType="relationship"][inherited]').style('display', 'none');

// Toggle inherited relationship edges
function toggleInheritedEdges(show) {{
  const edges = cy.edges('[edgeType="relationship"][inherited]');
  edges.style('display', show ? 'element' : 'none');
}}

// Spacing control
function applySpacing(value) {{
  const v = parseInt(value) / 100;
  const nodeSep = 15 + v * 120;
  const rankSep = 25 + v * 150;
  const edgeSep = 8 + v * 50;
  cy.layout({{
    name: 'dagre',
    rankDir: 'TB',
    nodeSep: nodeSep,
    rankSep: rankSep,
    edgeSep: edgeSep,
    ranker: 'network-simplex',
    animate: true,
    animationDuration: 400,
    animationEasing: 'ease-in-out-cubic',
  }}).run();
}}

// Tooltip
const tooltip = document.getElementById('tooltip');

function formatProps(props, prefix, cssClass) {{
  return props.map(p => `<span class="${{cssClass}}">${{prefix}}${{p}}</span>`).join('<br>');
}}

// Highlight on hover
const HL_BORDER = 10;
const HL_EDGE = 8;
const HL_COLOR = '#ffdd57';
const DIM_OPACITY = 0.12;

function dimMode() {{
  return document.getElementById('toggleDimMode').checked;
}}

function highlightNode(node) {{
  const connected = node.connectedEdges().filter(e => e.style('display') !== 'none');
  const neighbors = connected.connectedNodes();
  if (dimMode()) {{
    cy.elements().style('opacity', DIM_OPACITY);
    node.style({{ 'opacity': 1, 'border-width': HL_BORDER, 'border-color': HL_COLOR }});
    connected.style({{ 'opacity': 1, 'width': HL_EDGE }});
    neighbors.style({{ 'opacity': 1, 'border-width': HL_BORDER, 'border-color': HL_COLOR }});
  }} else {{
    node.style({{ 'border-width': HL_BORDER, 'border-color': HL_COLOR }});
    connected.style({{ 'width': HL_EDGE }});
    neighbors.style({{ 'border-width': HL_BORDER, 'border-color': HL_COLOR }});
  }}
}}

function highlightEdge(edge) {{
  if (dimMode()) {{
    cy.elements().style('opacity', DIM_OPACITY);
    edge.style({{ 'opacity': 1, 'width': HL_EDGE }});
    edge.source().style({{ 'opacity': 1, 'border-width': HL_BORDER, 'border-color': HL_COLOR }});
    edge.target().style({{ 'opacity': 1, 'border-width': HL_BORDER, 'border-color': HL_COLOR }});
  }} else {{
    edge.style({{ 'width': HL_EDGE }});
    edge.source().style({{ 'border-width': HL_BORDER, 'border-color': HL_COLOR }});
    edge.target().style({{ 'border-width': HL_BORDER, 'border-color': HL_COLOR }});
  }}
}}

function clearHighlight() {{
  cy.elements().style('opacity', 1);
  cy.nodes('[type="element"]').style({{ 'border-width': 2, 'border-color': isDark ? darkTheme.nodeBorder : lightTheme.nodeBorder }});
  cy.edges('[edgeType="relationship"]').style('width', 2.5);
  cy.edges('[edgeType="ownership"]').style('width', 1.5);
  cy.edges('[edgeType="typing"]').style('width', 1);
  cy.edges('[edgeType="extends"]').style('width', 2);
  if (!document.getElementById('toggleInherited').checked) {{
    cy.edges('[edgeType="relationship"][inherited]').style('display', 'none');
  }}
}}

cy.on('mouseover', 'node', function(e) {{
  highlightNode(e.target);
}});
cy.on('mouseover', 'edge', function(e) {{
  highlightEdge(e.target);
}});
cy.on('mouseout', 'node, edge', function() {{
  clearHighlight();
}});

// Element tooltip
cy.on('mouseover', 'node[type="element"]', function(e) {{
  if (!document.getElementById('toggleElementHover').checked) return;
  const d = e.target.data();
  let html = `<div class="tt-name">${{d.label}}</div>`;

  const idProps = (d.id_properties || []).map(p => `<span class="tt-key"><span class="tt-icon">🔑</span>${{p}}</span>`);
  const inhIdProps = (d.inherited_id_properties || []).map(p => `<span class="tt-inherited tt-key"><span class="tt-icon">🔑</span>${{p}} ↑</span>`);
  if (idProps.length || inhIdProps.length) {{
    html += `<div class="tt-section"><div class="tt-section-title">ID Properties</div>${{[...idProps, ...inhIdProps].join('<br>')}}</div>`;
  }}

  const props = (d.properties || []).map(p => `<span class="tt-icon">📌</span>${{p}}`);
  const inhProps = (d.inherited_properties || []).map(p => `<span class="tt-inherited"><span class="tt-icon">📌</span>${{p}} ↑</span>`);
  if (props.length || inhProps.length) {{
    html += `<div class="tt-section"><div class="tt-section-title">Properties</div>${{[...props, ...inhProps].join('<br>')}}</div>`;
  }}

  const optProps = (d.optional_properties || []).map(p => `<span class="tt-opt"><span class="tt-icon">❓</span>${{p}}</span>`);
  const inhOptProps = (d.inherited_optional_properties || []).map(p => `<span class="tt-inherited tt-opt"><span class="tt-icon">❓</span>${{p}} ↑</span>`);
  if (optProps.length || inhOptProps.length) {{
    html += `<div class="tt-section"><div class="tt-section-title">Optional</div>${{[...optProps, ...inhOptProps].join('<br>')}}</div>`;
  }}

  if (d.owners && d.owners.length) {{
    html += `<div class="tt-section"><div class="tt-section-title">Owned by</div>${{d.owners.join(', ')}}</div>`;
  }}
  if (d.typed_by && d.typed_by.length) {{
    html += `<div class="tt-section"><div class="tt-section-title">Typed by</div>${{d.typed_by.join(', ')}}</div>`;
  }}
  if (d.extends) {{
    html += `<div class="tt-section"><div class="tt-section-title">Extends</div>${{d.extends}}</div>`;
  }}

  // Show relationships (direct + inherited in one section)
  const allRelEdges = e.target.connectedEdges('[edgeType="relationship"]');
  if (allRelEdges.length > 0) {{
    const rels = [];
    allRelEdges.forEach(edge => {{
      const ed = edge.data();
      const isSource = ed.source === d.id;
      const other = isSource ? ed.target : ed.source;
      const dirWord = isSource ? '<span class="tt-dir">to</span>' : '<span class="tt-dir">from</span>';
      // Only show ↑ if this node is on the inherited side
      const isInhForMe = ed.inherited && (
        ed.inheritedSide === 'both' ||
        (isSource && ed.inheritedSide === 'source') ||
        (!isSource && ed.inheritedSide === 'destination')
      );
      const suffix = isInhForMe ? ' <span class="tt-inherited">↑</span>' : '';
      rels.push(`${{ed.relName}} ${{dirWord}} ${{other}}${{suffix}}`);
    }});
    html += `<div class="tt-section"><div class="tt-section-title">Relationships</div>${{rels.join('<br>')}}</div>`;
  }}

  tooltip.innerHTML = html;
  tooltip.style.display = 'block';
}});

// Relationship edge tooltip
cy.on('mouseover', 'edge', function(e) {{
  if (!document.getElementById('toggleRelHover').checked) return;
  const d = e.target.data();
  const src = d.source;
  const tgt = d.target;
  let html = `<div class="tt-name">${{d.label || d.edgeType}}</div>`;
  html += `<div class="tt-section">${{src}} &rarr; ${{tgt}}</div>`;
  if (d.edgeType === 'relationship') {{
    if (d.inherited) {{
      html += `<div class="tt-section"><span class="tt-inherited">Inherited relationship</span></div>`;
    }}
  }}
  tooltip.innerHTML = html;
  tooltip.style.display = 'block';
}});

// Tooltip positioning
cy.on('mousemove', function(e) {{
  if (tooltip.style.display === 'block') {{
    const x = e.originalEvent.clientX;
    const y = e.originalEvent.clientY;
    const rect = tooltip.getBoundingClientRect();
    const maxX = window.innerWidth - rect.width - 20;
    const maxY = window.innerHeight - rect.height - 20;
    tooltip.style.left = Math.min(x + 15, maxX) + 'px';
    tooltip.style.top = Math.min(y + 15, maxY) + 'px';
  }}
}});

cy.on('mouseout', 'node, edge', function() {{
  tooltip.style.display = 'none';
  clearHighlight();
  // Re-apply search highlight if active
  if (searchMatches.length > 0) applySearchHighlight();
}});

// Search
let searchMatches = [];
let searchIndex = 0;

function getSearchScopes() {{
  const checks = document.querySelectorAll('.search-scope');
  const scopes = {{}};
  checks.forEach(c => scopes[c.value] = c.checked);
  return scopes;
}}

function toggleSearchFilters(e) {{
  e.stopPropagation();
  const panel = document.getElementById('searchFilters');
  panel.classList.toggle('open');
  if (panel.classList.contains('open')) {{
    const btn = e.currentTarget.getBoundingClientRect();
    panel.style.top = (btn.bottom + 8) + 'px';
  }}
}}
document.addEventListener('click', function(e) {{
  const panel = document.getElementById('searchFilters');
  if (panel && panel.classList.contains('open') && !panel.contains(e.target)) {{
    panel.classList.remove('open');
  }}
}});
// Re-search when scope checkboxes change
document.querySelectorAll('.search-scope').forEach(c => {{
  c.addEventListener('change', () => onSearch(document.getElementById('searchInput').value));
}});

let searchTimer = null;
function onSearch(query) {{
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => executeSearch(query), 250);
}}

function executeSearch(query) {{
  const clearBtn = document.getElementById('searchClear');
  const counter = document.getElementById('searchCounter');
  clearBtn.style.display = query ? 'block' : 'none';

  if (!query) {{
    searchMatches = [];
    searchIndex = 0;
    counter.textContent = '';
    clearSearchHighlight();
    return;
  }}

  const q = query.toLowerCase();
  const scopes = getSearchScopes();
  const matches = new Set();

  cy.nodes().forEach(node => {{
    const d = node.data();
    const isElem = d.type === 'element';
    // Element names
    if (scopes.elements && isElem && d.label.toLowerCase().includes(q)) {{
      matches.add(node.id());
    }}
    // Properties
    if (scopes.properties && isElem) {{
      const allProps = [
        ...(d.id_properties || []), ...(d.properties || []), ...(d.optional_properties || []),
        ...(d.inherited_id_properties || []), ...(d.inherited_properties || []), ...(d.inherited_optional_properties || [])
      ];
      if (allProps.some(p => p.toLowerCase().includes(q))) matches.add(node.id());
    }}
    // Types
    // Typed-by (check if any typed_by values match)
    if (scopes.types && isElem && d.typed_by) {{
      if (d.typed_by.some(t => t.toLowerCase().includes(q))) matches.add(node.id());
    }}
  }});

  // Relationships
  if (scopes.relationships) {{
    cy.edges().forEach(edge => {{
      const d = edge.data();
      if (d.label && d.label.toLowerCase().includes(q)) {{
        matches.add(edge.source().id());
        matches.add(edge.target().id());
      }}
    }});
  }}

  searchMatches = [...matches].map(id => cy.getElementById(id)).filter(n => n.length > 0);
  searchIndex = 0;

  if (searchMatches.length > 0) {{
    applySearchHighlight();
    panToMatch();
    counter.textContent = `${{searchIndex + 1}}/${{searchMatches.length}}`;
  }} else {{
    clearSearchHighlight();
    counter.textContent = '0 results';
  }}
}}

function applySearchHighlight() {{
  cy.elements().style('opacity', DIM_OPACITY);
  searchMatches.forEach(node => {{
    node.style({{ 'opacity': 1, 'border-width': HL_BORDER, 'border-color': HL_COLOR }});
    const connected = node.connectedEdges().filter(e => e.style('display') !== 'none');
    connected.style({{ 'opacity': 1, 'width': HL_EDGE }});
    connected.connectedNodes().style('opacity', 0.6);
  }});
  // Ensure matched nodes are fully opaque (override neighbor dimming)
  searchMatches.forEach(node => node.style('opacity', 1));
  // Current match extra emphasis
  if (searchMatches[searchIndex]) {{
    searchMatches[searchIndex].style({{ 'border-color': '#ff6b6b' }});
  }}
}}

function panToMatch() {{
  if (searchMatches.length === 0) return;
  const node = searchMatches[searchIndex];
  cy.animate({{
    center: {{ eles: node }},
    zoom: cy.zoom(),
    duration: 300,
    easing: 'ease-in-out-cubic',
  }});
}}

function clearSearchHighlight() {{
  cy.elements().style('opacity', 1);
  cy.nodes('[type="element"]').style({{ 'border-width': 2, 'border-color': isDark ? darkTheme.nodeBorder : lightTheme.nodeBorder }});
  cy.edges('[edgeType="relationship"]').style('width', 2.5);
  cy.edges('[edgeType="ownership"]').style('width', 1.5);
  cy.edges('[edgeType="typing"]').style('width', 1);
  cy.edges('[edgeType="extends"]').style('width', 2);
  if (!document.getElementById('toggleInherited').checked) {{
    cy.edges('[edgeType="relationship"][inherited]').style('display', 'none');
  }}
}}

function clearSearch() {{
  document.getElementById('searchInput').value = '';
  document.getElementById('searchClear').style.display = 'none';
  document.getElementById('searchCounter').textContent = '';
  searchMatches = [];
  searchIndex = 0;
  clearSearchHighlight();
}}

function onSearchKey(e) {{
  if (e.key === 'Enter' && searchMatches.length > 1) {{
    searchIndex = (searchIndex + (e.shiftKey ? -1 : 1) + searchMatches.length) % searchMatches.length;
    applySearchHighlight();
    panToMatch();
    document.getElementById('searchCounter').textContent = `${{searchIndex + 1}}/${{searchMatches.length}}`;
  }}
  if (e.key === 'Escape') {{
    clearSearch();
    e.target.blur();
  }}
}}
</script>
</body>
</html>"""


def main():
    args = parse_args()
    metamodel = load_metamodel(args.input)
    validate_metamodel(metamodel)

    resolved, parent_of = resolve_inheritance(metamodel["elements"])
    relationships = resolve_relationship_inheritance(metamodel["relationships"], metamodel["elements"], parent_of)
    cy_elements, rel_colors = build_graph_data(resolved, relationships, parent_of)
    html = generate_html(cy_elements, rel_colors)

    output_path = Path(args.output)
    output_path.write_text(html)
    print(f"Visualization written to {output_path}")


if __name__ == "__main__":
    main()
