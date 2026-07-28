"""
🐝 BeHive CLI — State-of-the-art animated research visualization.

A cinematic terminal experience showing the research pipeline as a
living bee colony: scout bees navigate the web, harvest bees extract
knowledge, worker bees refine claims, and the queen synthesizes
the final intelligence report.

Features:
- Full-screen Rich TUI with smooth 12fps animations
- Particle system: pollen/data flowing between pipeline stages
- ASCII art bees with flight physics (sine-wave, acceleration)
- Real-time knowledge graph growing (ASCII nodes + edges)
- Honeycomb progress with gradient fill
- Cinematic phase transitions
- Quality score climbing in real-time
- Stats dashboard with sparkline history
"""
import time
import math
import random
import sys
import os
from dataclasses import dataclass, field
from typing import List, Tuple

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.align import Align
from rich.rule import Rule
from rich.columns import Columns
from rich import box

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

WIDTH = 78  # Terminal columns
FPS = 10
FRAME_MS = 1.0 / FPS

# Color palette — warm amber/gold theme
C_AMBER = "#FFB300"
C_GOLD = "#FFC107"
C_HONEY = "#FF8F00"
C_WARM = "#FFD54F"
C_BROWN = "#5D4037"
C_GREEN = "#66BB6A"
C_BLUE = "#4FC3F7"
C_PURPLE = "#AB47BC"
C_CORAL = "#FF7043"
C_DIM = "#616161"

# Unicode blocks
BLOCK_FULL = "█"
BLOCK_34 = "▓"
BLOCK_12 = "▒"
BLOCK_14 = "░"
CELL_F = "⬢"
CELL_E = "⬡"

console = Console(width=WIDTH, highlight=False)


# ═══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Particle:
    x: float
    y: float
    vx: float
    vy: float
    char: str
    life: int
    color: str


@dataclass
class Bee:
    x: float
    y: float
    target_x: float
    speed: float
    carrying: bool = False
    wobble_phase: float = 0.0
    
    def update(self, dt: float = 1.0):
        dx = self.target_x - self.x
        self.x += dx * self.speed * dt * 0.15
        self.wobble_phase += 0.3
        self.y = math.sin(self.wobble_phase) * 0.4


@dataclass 
class SimState:
    """Full simulation state for the research pipeline."""
    tick: int = 0
    phase: str = "intro"
    phase_start: float = 0.0
    
    # Scout
    queries: int = 0
    sources: int = 0
    apis: int = 0
    
    # Harvest
    fetched: int = 0
    total_docs: int = 87
    content_mb: float = 0.0
    
    # Process
    docs_done: int = 0
    docs_total: int = 64
    claims: int = 0
    quality: float = 0.0
    entities: int = 0
    quality_history: List[float] = field(default_factory=list)
    
    # Synth
    relations: int = 0
    report_sections: int = 0
    
    # Animation
    bees: List[Bee] = field(default_factory=list)
    particles: List[Particle] = field(default_factory=list)
    log_lines: List[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════════
# RENDERING PRIMITIVES
# ═══════════════════════════════════════════════════════════════════════════════

def gradient_text(text: str, colors: List[str]) -> Text:
    """Apply a color gradient across text characters."""
    t = Text()
    n = len(colors)
    for i, ch in enumerate(text):
        ci = int(i / max(len(text) - 1, 1) * (n - 1))
        t.append(ch, style=colors[min(ci, n-1)])
    return t


def sparkline(values: List[float], width: int = 20, 
              min_val: float = 0.0, max_val: float = 1.0) -> str:
    """Unicode sparkline chart."""
    if not values:
        return "▁" * width
    
    chars = "▁▂▃▄▅▆▇█"
    # Take last `width` values
    data = values[-width:]
    result = []
    for v in data:
        normalized = (v - min_val) / max(max_val - min_val, 0.001)
        idx = int(normalized * (len(chars) - 1))
        idx = max(0, min(idx, len(chars) - 1))
        result.append(chars[idx])
    
    # Pad left if not enough data
    padding = width - len(result)
    return "▁" * padding + "".join(result)


def honeycomb_progress(pct: float, width: int = 28, tick: int = 0) -> str:
    """Animated honeycomb progress bar with glow effect."""
    filled = int(pct * width)
    result = []
    
    for i in range(width):
        if i < filled - 1:
            result.append(f"[{C_GOLD}]{CELL_F}[/]")
        elif i == filled - 1 and filled > 0:
            # Leading edge glows
            glow = C_AMBER if tick % 3 != 0 else C_HONEY
            result.append(f"[bold {glow}]{CELL_F}[/]")
        elif i == filled and pct < 1.0:
            # Active cell being filled
            if tick % 2 == 0:
                result.append(f"[{C_HONEY}]{CELL_F}[/]")
            else:
                result.append(f"[dim]{CELL_E}[/]")
        else:
            result.append(f"[#424242]{CELL_E}[/]")
    
    return "".join(result)


def quality_gauge(score: float, width: int = 22) -> str:
    """Premium quality gauge with color zones."""
    filled = int(score * width)
    result = []
    
    for i in range(width):
        threshold = i / width
        if i < filled:
            if threshold >= 0.82:
                result.append(f"[bold green]{BLOCK_FULL}[/]")
            elif threshold >= 0.75:
                result.append(f"[{C_GREEN}]{BLOCK_FULL}[/]")
            elif threshold >= 0.65:
                result.append(f"[{C_GOLD}]{BLOCK_34}[/]")
            else:
                result.append(f"[{C_CORAL}]{BLOCK_12}[/]")
        else:
            result.append(f"[#2a2a2a]{BLOCK_14}[/]")
    
    # Target marker at 0.82
    marker_pos = int(0.82 * width)
    # Return as string with marker annotation
    bar = "".join(result)
    return bar


def render_bees_flight(bees: List[Bee], width: int = 60, 
                       targets: List[str] = None) -> List[str]:
    """Render multiple bees in flight across a field."""
    lines = []
    # 3 flight lanes
    for lane in range(3):
        field = [" "] * width
        
        # Place bees in this lane
        for i, bee in enumerate(bees):
            if i % 3 == lane:
                bx = int(bee.x)
                if 0 <= bx < width:
                    # Trail behind bee
                    trail_len = 3 if not bee.carrying else 2
                    for t in range(1, trail_len + 1):
                        tp = bx - t if not bee.carrying else bx + t
                        if 0 <= tp < width:
                            field[tp] = "·"
                    
                    field[bx] = "🐝" if not bee.carrying else "🐝"
        
        # Place targets on right edge
        if targets and lane < len(targets):
            target = targets[lane]
            field[-1] = target
        
        lines.append("  " + "".join(field))
    
    return lines


def render_data_flow(tick: int, width: int = 60) -> str:
    """Animated data particles flowing left to right."""
    chars = ["·", "∘", "○", "●", "◉"]
    line = [" "] * width
    
    for i in range(8):
        pos = (tick * 2 + i * 7) % width
        char_idx = (tick + i) % len(chars)
        if 0 <= pos < width:
            line[pos] = chars[char_idx]
    
    return "".join(line)


def mini_graph(entities: List[str], relations: int, width: int = 50) -> List[str]:
    """ASCII knowledge graph visualization."""
    if not entities:
        return ["  [dim](empty graph)[/dim]"]
    
    lines = []
    
    # Top row of nodes
    row1 = entities[:4]
    row2 = entities[4:7] if len(entities) > 4 else []
    
    # Draw connections
    if len(row1) >= 2:
        node_strs = [f"[bold]({e})[/]" for e in row1]
        line = "───".join(node_strs)
        lines.append(f"  {line}")
    
    if row2:
        # Vertical connections
        spacing = "      │" * min(len(row2), 3)
        lines.append(f"  {spacing}")
        node_strs = [f"[bold]({e})[/]" for e in row2]
        line = "───".join(node_strs)
        lines.append(f"       {line}")
    
    return lines


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE RENDERERS  
# ═══════════════════════════════════════════════════════════════════════════════

def render_intro(state: SimState) -> Table:
    """Cinematic intro — BeHive logo reveal."""
    t = state.tick
    output = Table.grid(padding=0)
    
    # Honeycomb ASCII art logo
    logo_lines = [
        "         ⬡ ⬢ ⬡ ⬢ ⬡ ⬢ ⬡ ⬢ ⬡ ⬢ ⬡ ⬢ ⬡",
        "        ⬢ ⬡ ⬢ ⬡ ⬢ ⬡ ⬢ ⬡ ⬢ ⬡ ⬢ ⬡ ⬢ ⬡",
        "       ⬡ ⬢ ⬡ ⬢ ⬡ ⬢ ⬡ ⬢ ⬡ ⬢ ⬡ ⬢ ⬡ ⬢ ⬡",
        "        ⬢ ⬡ ⬢ ⬡ ⬢ ⬡ ⬢ ⬡ ⬢ ⬡ ⬢ ⬡ ⬢ ⬡",
        "         ⬡ ⬢ ⬡ ⬢ ⬡ ⬢ ⬡ ⬢ ⬡ ⬢ ⬡ ⬢ ⬡",
    ]
    
    # Reveal line by line
    visible = min(t // 2, len(logo_lines))
    
    content = Text()
    content.append("\n")
    for i in range(visible):
        style = C_GOLD if (i + t) % 2 == 0 else C_AMBER
        content.append(f"  {logo_lines[i]}\n", style=style)
    
    content.append("\n")
    
    # Title reveal
    if t > 6:
        title = "B e H i v e"
        reveal_chars = min(t - 6, len(title))
        content.append("              ", style="")
        content.append(title[:reveal_chars], style=f"bold {C_AMBER}")
        content.append("\n")
    
    if t > 10:
        content.append("              Deep Research Engine\n", style="dim italic")
    
    if t > 13:
        content.append(f"\n  [dim]v0.1.0 — Structured knowledge from any topic[/dim]\n")
    
    if t > 16:
        content.append(f"\n")
        content.append(f"  🐝  Initializing swarm", style=C_GOLD)
        dots = "." * ((t - 16) % 4)
        content.append(dots, style="dim")
        content.append("\n")
    
    output.add_row(Panel(content, border_style=C_AMBER, box=box.DOUBLE, 
                         padding=(1, 2)))
    return output


def render_scout(state: SimState) -> Table:
    """Scout phase — full panel with live metrics."""
    t = state.tick
    output = Table.grid(padding=0)
    
    # Header
    elapsed = time.time() - state.phase_start
    header = Text()
    header.append(f"  🔍 SCOUT", style=f"bold {C_GREEN}")
    header.append(f"  │  ", style="dim")
    header.append(f"{elapsed:.1f}s", style="dim")
    header.append(f"  │  ", style="dim")
    header.append(f"Dispatching bees to find sources", style="italic dim")
    output.add_row(header)
    output.add_row(Text(""))
    
    # Bee flight visualization
    lines = []
    lines.append("")
    
    # Multiple bees with targets
    for bee in state.bees:
        bee.update()
    
    targets = ["🌐", "📄", "🔬", "📰", "📊"]
    flight_lines = render_bees_flight(state.bees, width=55, targets=targets[:len(state.bees)])
    for fl in flight_lines:
        lines.append(fl)
    
    lines.append("")
    
    # Data flow animation
    flow = render_data_flow(t, width=55)
    lines.append(f"  [{C_DIM}]{flow}[/]")
    lines.append("")
    
    # Waggle dance communication
    if t > 10:
        dances = [
            f"  [dim]Bee 1 found 12 papers on arxiv.org[/dim]",
            f"  [dim]Bee 2 found 8 reports on reuters.com[/dim]",
            f"  [dim]Bee 3 querying SEC EDGAR filings[/dim]",
            f"  [dim]Bee 4 scanning patent databases[/dim]",
        ]
        visible_dances = min((t - 10) // 4, len(dances))
        for d in dances[:visible_dances]:
            lines.append(d)
        lines.append("")
    
    # Stats
    lines.append(f"  ┌────────────────────────────────────────────────────┐")
    lines.append(f"  │  🔍 Search queries:  [{C_GOLD}]{state.queries:>3}[/]  generated               │")
    lines.append(f"  │  🌐 Sources found:   [{C_GOLD}]{state.sources:>3}[/]  unique URLs              │")
    lines.append(f"  │  📡 API endpoints:   [{C_GOLD}]{state.apis:>3}[/]  checked                  │")
    lines.append(f"  │  🐝 Active scouts:   [{C_GOLD}]{len(state.bees):>3}[/]  deployed                 │")
    lines.append(f"  └────────────────────────────────────────────────────┘")
    
    content = "\n".join(lines)
    output.add_row(Panel(content, border_style=C_GREEN, box=box.ROUNDED, 
                         title=f"[bold {C_GREEN}]Scout Phase[/]", padding=(0, 1)))
    return output


def render_harvest(state: SimState) -> Table:
    """Harvest phase — bees returning with documents."""
    t = state.tick
    output = Table.grid(padding=0)
    
    elapsed = time.time() - state.phase_start
    header = Text()
    header.append(f"  🌸 HARVEST", style=f"bold {C_BLUE}")
    header.append(f"  │  ", style="dim")
    header.append(f"{elapsed:.1f}s", style="dim")
    header.append(f"  │  ", style="dim")
    header.append(f"Collecting documents (nectar)", style="italic dim")
    output.add_row(header)
    output.add_row(Text(""))
    
    pct = state.fetched / max(state.total_docs, 1)
    lines = []
    lines.append("")
    
    # Bees returning with documents (flying right to left)
    for i, bee in enumerate(state.bees[:4]):
        bee.target_x = 2  # Return to hive
        bee.update()
        bx = max(2, min(int(55 - bee.x * 0.8), 54))
        
        lane = [" "] * 56
        # Nectar trail
        for trail in range(1, 4):
            tp = bx + trail
            if tp < 56:
                lane[tp] = "·"
        lane[bx] = "🐝"
        lane[1] = "⬢"  # Hive entrance
        
        cargo = "📄" if i % 2 == 0 else "🔗"
        lines.append(f"  {cargo}{''.join(lane)}")
    
    lines.append("")
    
    # Progress bar
    bar = honeycomb_progress(pct, width=30, tick=t)
    lines.append(f"  {bar}  [{C_GOLD}]{int(pct*100)}%[/]")
    lines.append("")
    
    # Document stream (recent fetches)
    domains = [
        ("arxiv.org", "2501.08471 — Scaling Laws for AI Chips"),
        ("reuters.com", "NVIDIA Q4 earnings beat estimates"),
        ("sec.gov", "AMD 10-K Annual Report FY2024"),
        ("nature.com", "s41586-025-08192 — Photonic computing"),
        ("semianalysis.com", "H200 vs MI325X inference benchmark"),
        ("anandtech.com", "Intel Gaudi 3 deep dive"),
        ("techcrunch.com", "TSMC 2nm timeline confirmed"),
        ("ieee.org", "Survey: AI accelerator architectures"),
    ]
    
    lines.append(f"  [bold]Recent:[/bold]")
    num_visible = min((t // 3) + 1, 4)
    for i in range(num_visible):
        idx = (t // 3 + i) % len(domains)
        domain, title = domains[idx]
        status = "[green]✓[/]" if i < num_visible - 1 else f"[{C_GOLD}]↓[/]"
        lines.append(f"    {status} [{C_DIM}]{domain}[/] — {title[:38]}")
    
    lines.append("")
    lines.append(f"  ┌─────────────────────────────────────────────────┐")
    lines.append(f"  │  📄 Documents:  [{C_GOLD}]{state.fetched:>3}[/] / {state.total_docs}  fetched          │")
    lines.append(f"  │  💾 Content:    [{C_GOLD}]{state.content_mb:>5.1f}[/] MB  extracted          │")
    lines.append(f"  │  ❌ Failed:     [{C_DIM}]{state.total_docs - state.fetched - max(0, state.total_docs - state.fetched - 3):>3}[/]      skipped              │")
    lines.append(f"  └─────────────────────────────────────────────────┘")
    
    content = "\n".join(lines)
    output.add_row(Panel(content, border_style=C_BLUE, box=box.ROUNDED,
                         title=f"[bold {C_BLUE}]Harvest Phase[/]", padding=(0, 1)))
    return output


def render_process(state: SimState) -> Table:
    """Process phase — the main extraction engine."""
    t = state.tick
    output = Table.grid(padding=0)
    
    elapsed = time.time() - state.phase_start
    pct = state.docs_done / max(state.docs_total, 1)
    
    header = Text()
    header.append(f"  ⚗️  PROCESS", style=f"bold {C_AMBER}")
    header.append(f"  │  ", style="dim")
    header.append(f"{elapsed:.1f}s", style="dim")
    header.append(f"  │  ", style="dim")
    header.append(f"Extracting claims (making honey)", style="italic dim")
    output.add_row(header)
    output.add_row(Text(""))
    
    lines = []
    lines.append("")
    
    # Worker bees with current activity
    num_workers = min(6, max(2, state.docs_done // 8))
    activities = ["extract", "score", "enrich", "dedup", "validate", "link"]
    worker_line = ""
    for w in range(num_workers):
        act = activities[w % len(activities)]
        if (t + w) % 12 < 6:
            worker_line += f"🐝[dim]({act})[/] "
        else:
            worker_line += f"🐝[dim]({act})[/] "
    lines.append(f"  Workers: {worker_line}")
    lines.append("")
    
    # Dual progress: documents + quality
    prog_bar = honeycomb_progress(pct, width=24, tick=t)
    q_bar = quality_gauge(state.quality, width=20)
    
    lines.append(f"  Progress  {prog_bar}  [{C_GOLD}]{int(pct*100):>3}%[/]")
    lines.append(f"  Quality   {q_bar}  [bold]{state.quality:.3f}[/]")
    
    # Quality sparkline
    spark = sparkline(state.quality_history, width=24, min_val=0.5, max_val=1.0)
    lines.append(f"  History   [{C_DIM}]{spark}[/]  [dim]▁0.5 ▅0.75 █1.0[/dim]")
    lines.append("")
    
    # V4 Pipeline stages
    lines.append(f"  [bold]Pipeline:[/bold]")
    stages = [
        ("BeeHive extraction", pct >= 0.0, pct >= 0.35, "conf gate 0.75"),
        ("V4 Haiku bulk", pct >= 0.35, pct >= 0.70, f"{min(state.claims, 280)} claims"),
        ("Sonnet enrichment", pct >= 0.70, pct >= 0.90, "upgrading thin claims"),
        ("Deduplication", pct >= 0.90, pct >= 1.0, "Jaccard 0.60"),
    ]
    
    for name, started, done, detail in stages:
        if done:
            lines.append(f"    [green]✓[/] {name} [dim]— {detail}[/dim]")
        elif started:
            spinner = ["◐", "◓", "◑", "◒"][t % 4]
            lines.append(f"    [{C_GOLD}]{spinner}[/] [bold]{name}[/bold] [dim]— {detail}[/dim]")
        else:
            lines.append(f"    [dim]○ {name} — {detail}[/dim]")
    
    lines.append("")
    
    # Stats box
    above_082 = int(state.claims * min(pct, 1.0) * 0.35) if state.quality > 0.78 else int(state.claims * 0.15)
    lines.append(f"  ┌─────────────────────────────────────────────────┐")
    lines.append(f"  │  📋 Claims:     [{C_GOLD}]{state.claims:>5}[/]  extracted                │")
    lines.append(f"  │  🏷️  Entities:   [{C_GOLD}]{state.entities:>5}[/]  discovered               │")
    lines.append(f"  │  ⭐ Above 0.82: [green]{above_082:>5}[/]  high-confidence          │")
    lines.append(f"  │  📄 Docs:       [{C_GOLD}]{state.docs_done:>3}[/] / {state.docs_total}  processed             │")
    lines.append(f"  └─────────────────────────────────────────────────┘")
    
    content = "\n".join(lines)
    output.add_row(Panel(content, border_style=C_AMBER, box=box.ROUNDED,
                         title=f"[{C_AMBER}]Process Phase[/]", padding=(0, 1)))
    return output


def render_synth(state: SimState) -> Table:
    """Synthesis phase — Queen building the final report."""
    t = state.tick
    output = Table.grid(padding=0)
    
    elapsed = time.time() - state.phase_start
    header = Text()
    header.append(f"  👑 SYNTHESIZE", style=f"bold {C_PURPLE}")
    header.append(f"  │  ", style="dim")
    header.append(f"{elapsed:.1f}s", style="dim")
    header.append(f"  │  ", style="dim")
    header.append(f"Queen building report & knowledge graph", style="italic dim")
    output.add_row(header)
    output.add_row(Text(""))
    
    lines = []
    lines.append("")
    
    # Queen bee animation
    queen_states = [
        "  👑🐝  analyzing claim relationships...",
        "  👑🐝  cross-referencing sources...",
        "  👑🐝  building entity graph...",
        "  👑🐝  synthesizing narrative...",
        "  👑🐝  writing executive summary...",
        "  👑🐝  scoring confidence levels...",
    ]
    lines.append(f"  [{C_PURPLE}]{queen_states[t % len(queen_states)]}[/]")
    lines.append("")
    
    # Knowledge graph growing
    entity_names = ["NVIDIA", "H200", "AMD", "MI325X", "TSMC", "Intel", 
                    "Gaudi3", "3nm", "HBM3e", "Blackwell"]
    visible_entities = entity_names[:min(state.entities // 5 + 1, len(entity_names))]
    
    lines.append(f"  [bold]Knowledge Graph:[/bold]  [dim]+{state.entities} entities, +{state.relations} edges[/dim]")
    graph_lines = mini_graph(visible_entities, state.relations, width=55)
    for gl in graph_lines:
        lines.append(gl)
    lines.append("")
    
    # Report sections being written
    section_names = [
        "Executive Summary", "Market Overview", "NVIDIA Analysis",
        "AMD Analysis", "Intel Analysis", "TSMC Supply Chain",
        "Competitive Dynamics", "Price Trends", "Future Outlook",
        "Risk Factors", "Sources & Confidence", "Appendix"
    ]
    
    lines.append(f"  [bold]Report:[/bold]  {state.report_sections}/12 sections")
    
    # Section progress
    section_bar = ""
    for i in range(12):
        if i < state.report_sections:
            section_bar += f"[green]█[/]"
        elif i == state.report_sections:
            section_bar += f"[{C_GOLD}]▓[/]"
        else:
            section_bar += f"[dim]░[/]"
    lines.append(f"  [{section_bar}]")
    
    if state.report_sections > 0:
        latest = section_names[min(state.report_sections - 1, len(section_names) - 1)]
        lines.append(f"  [dim]Latest: {latest}[/dim]")
    
    lines.append("")
    
    # Final stats
    lines.append(f"  ┌─────────────────────────────────────────────────┐")
    lines.append(f"  │  📋 Total claims:   [bold]{state.claims:>5}[/bold]                       │")
    lines.append(f"  │  📊 Avg quality:    [bold green]{state.quality:.3f}[/]                       │")
    lines.append(f"  │  🕸️  Graph edges:    [bold]{state.relations:>5}[/bold]                       │")
    lines.append(f"  │  📄 Report words:   [bold]{state.report_sections * 237:>5}[/bold]                       │")
    lines.append(f"  └─────────────────────────────────────────────────┘")
    
    content = "\n".join(lines)
    output.add_row(Panel(content, border_style=C_PURPLE, box=box.ROUNDED,
                         title=f"[bold {C_PURPLE}]Synthesis Phase[/]", padding=(0, 1)))
    return output


def render_done(state: SimState, duration: float, topic: str) -> Table:
    """Final completion — research harvested."""
    output = Table.grid(padding=0)
    
    # Honeycomb border
    border1 = " ".join([CELL_F if i % 2 == 0 else CELL_E for i in range(28)])
    border2 = " ".join([CELL_E if i % 2 == 0 else CELL_F for i in range(28)])
    
    slug = topic.replace(" ", "-").lower()[:30]
    above_082 = int(state.claims * 0.35)
    
    lines = [
        "",
        f"  [{C_GOLD}]{border1}[/]",
        "",
        f"  [bold green]✓ Research Complete[/]  —  All bees returned to hive 🍯",
        "",
        f"  ╔═══════════════════════╦══════════════════════════╗",
        f"  ║  📋 Claims:    [bold]{state.claims:>5}[/]  ║  📊 Quality:    [bold green]{state.quality:.3f}[/]   ║",
        f"  ║  ⭐ Excellent: [bold green]{above_082:>5}[/]  ║  🕸️  Relations:  [bold]{state.relations:>5}[/]   ║",
        f"  ║  🏷️  Entities:  [bold]{state.entities:>5}[/]  ║  📄 Sources:    [bold]   64[/]   ║",
        f"  ║  ⏱️  Duration:  [bold]{duration:>4.0f}s[/]  ║  🔬 Depth:      [bold]    3[/]   ║",
        f"  ╚═══════════════════════╩══════════════════════════╝",
        "",
        f"  [bold]Output:[/bold]",
        f"    📄  ./output/{slug}.md",
        f"    📊  ./output/{slug}.json    [dim](structured claims)[/dim]",
        f"    🕸️   Neo4j localhost:7687    [dim](knowledge graph)[/dim]",
        "",
        f"  [bold]Quality breakdown:[/bold]",
        f"    [green]█████████████████[/][{C_GOLD}]████[/][{C_CORAL}]█[/][dim]░░[/]  0.824 avg",
        f"    [dim]0.65    0.75    0.82  ↑target   1.0[/dim]",
        "",
        f"  [{C_GOLD}]{border2}[/]",
        "",
    ]
    
    content = "\n".join(lines)
    output.add_row(Panel(content, border_style="green", box=box.DOUBLE,
                         title="[bold green]🍯 HARVEST COMPLETE[/]",
                         subtitle=f"[dim]{slug}[/dim]",
                         padding=(0, 1)))
    return output


# ═══════════════════════════════════════════════════════════════════════════════
# SIMULATION ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def run_demo(topic: str = "AI chip market NVIDIA AMD Intel 2025"):
    """Run the full cinematic demo."""
    
    state = SimState()
    state.bees = [Bee(x=0, y=0, target_x=50, speed=1.0 + i*0.3) for i in range(5)]
    
    console.clear()
    start_time = time.time()
    
    # ─── INTRO (2s) ───────────────────────────────────────────────────────
    state.phase = "intro"
    with Live(console=console, refresh_per_second=FPS, transient=True) as live:
        for tick in range(20):
            state.tick = tick
            live.update(render_intro(state))
            time.sleep(FRAME_MS)
    
    # Brief pause + topic display
    console.clear()
    topic_text = Text()
    topic_text.append("\n  🎯 ", style="")
    topic_text.append(topic, style="bold white")
    topic_text.append("\n", style="")
    console.print(topic_text)
    time.sleep(0.3)
    console.clear()
    
    # ─── SCOUT (3.5s) ─────────────────────────────────────────────────────
    state.phase = "scout"
    state.phase_start = time.time()
    state.tick = 0
    
    with Live(console=console, refresh_per_second=FPS, transient=True) as live:
        for tick in range(35):
            state.tick = tick
            
            # Update scout state
            state.queries = min(tick // 2 + 1, 14)
            state.sources = min(tick * 3 + random.randint(0, 2), 92)
            state.apis = min(tick // 4, 9)
            
            # Update bee positions
            for i, bee in enumerate(state.bees):
                bee.target_x = min(tick * 2 + i * 3, 52)
                bee.update()
            
            live.update(render_scout(state))
            time.sleep(FRAME_MS)
    
    console.clear()
    
    # ─── HARVEST (4s) ─────────────────────────────────────────────────────
    state.phase = "harvest"
    state.phase_start = time.time()
    state.tick = 0
    
    # Reset bees for return journey
    state.bees = [Bee(x=50, y=0, target_x=2, speed=0.8 + i*0.2) for i in range(5)]
    
    with Live(console=console, refresh_per_second=FPS, transient=True) as live:
        for tick in range(40):
            state.tick = tick
            
            state.fetched = min(int(tick * 1.7), 64)
            state.content_mb = state.fetched * 0.019
            
            for bee in state.bees:
                bee.update()
            
            live.update(render_harvest(state))
            time.sleep(FRAME_MS)
    
    console.clear()
    
    # ─── PROCESS (7s) — the longest and most interesting phase ────────────
    state.phase = "process"
    state.phase_start = time.time()
    state.tick = 0
    
    with Live(console=console, refresh_per_second=FPS, transient=True) as live:
        for tick in range(70):
            state.tick = tick
            
            # Smooth progression curves
            progress = tick / 70.0
            state.docs_done = min(int(progress * 67), 64)
            state.claims = min(int(progress * progress * 500 + progress * 100), 363)
            
            # Quality rises with some realistic fluctuation
            base_q = 0.68 + progress * 0.155  # 0.68 → 0.835
            noise = math.sin(tick * 0.7) * 0.008 + math.cos(tick * 0.3) * 0.005
            state.quality = min(base_q + noise, 0.828)
            state.quality_history.append(state.quality)
            
            state.entities = min(int(progress * 48), 42)
            
            live.update(render_process(state))
            time.sleep(FRAME_MS)
    
    # Final quality settle
    state.quality = 0.824
    state.claims = 363
    state.entities = 42
    state.docs_done = 64
    console.clear()
    
    # ─── SYNTH (4s) ───────────────────────────────────────────────────────
    state.phase = "synth"
    state.phase_start = time.time()
    state.tick = 0
    
    with Live(console=console, refresh_per_second=FPS, transient=True) as live:
        for tick in range(40):
            state.tick = tick
            
            state.relations = min(tick // 2 + 1, 18)
            state.report_sections = min(tick // 3, 12)
            
            live.update(render_synth(state))
            time.sleep(FRAME_MS)
    
    state.relations = 18
    state.report_sections = 12
    console.clear()
    
    # ─── DONE (hold 4s) ──────────────────────────────────────────────────
    duration = 94.2  # Simulated real-world duration
    
    with Live(console=console, refresh_per_second=2) as live:
        live.update(render_done(state, duration, topic))
        time.sleep(4.0)
    
    console.print()


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    topic = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "AI chip market NVIDIA AMD Intel 2025"
    run_demo(topic)
