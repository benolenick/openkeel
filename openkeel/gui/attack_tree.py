"""Attack Tree visualization panel — renders the Treadstone v2 tree as an interactive graph.

Shows stones as nodes branching from a root, with hypotheses and attempts as
child nodes. Color-coded by status, clickable to show sheets/details.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QPointF, QRectF, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from openkeel.core.treadstone import (
    TreadstoneTree,
    StoneNode,
    Hypothesis,
    Attempt,
    CircuitBreaker,
    get_active_stone,
    tree_status_line,
    load_tree,
    save_tree,
)
def missions_dir() -> Path:
    """Return the base missions directory."""
    d = Path.home() / ".openkeel" / "missions"
    d.mkdir(parents=True, exist_ok=True)
    return d

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

ORANGE = "#FF6611"
DARK_BG = "#0d0d0d"
SCENE_BG = "#111111"
TEXT_DIM = "#888888"
TEXT_LIGHT = "#cccccc"
GRID_COLOR = "#1a1a1a"

# Node colors by status
STATUS_COLORS = {
    "pending": "#555555",
    "active": ORANGE,
    "done": "#22cc44",
    "failed": "#cc2222",
    "abandoned": "#cc2222",
    "succeeded": "#22cc44",
    "pivoted": "#cc8800",
    "partial": "#ccaa00",
}

PHASE_COLORS = {
    "recon": "#4488ff",
    "research": "#aa44ff",
    "run": ORANGE,
    "review": "#44ccaa",
}

# Layout
NODE_W = 140
NODE_H = 50
H_SPACING = 180
V_SPACING = 100
HYP_W = 120
HYP_H = 36
ATT_R = 10  # attempt dot radius


# ---------------------------------------------------------------------------
# Tree Node Items (QGraphicsItems)
# ---------------------------------------------------------------------------

class StoneNodeItem(QGraphicsRectItem):
    """Visual representation of a StoneNode in the tree."""

    def __init__(self, stone: StoneNode, x: float, y: float, parent=None):
        super().__init__(x, y, NODE_W, NODE_H, parent)
        self.stone = stone
        self.setAcceptHoverEvents(True)

        color = QColor(STATUS_COLORS.get(stone.status, "#555555"))
        self.setBrush(QBrush(color.darker(200)))
        self.setPen(QPen(color, 2))
        self.setZValue(10)

        # Phase indicator bar at top
        phase_color = QColor(PHASE_COLORS.get(stone.phase, ORANGE))
        phase_bar = QGraphicsRectItem(x + 2, y + 2, NODE_W - 4, 4, self)
        phase_bar.setBrush(QBrush(phase_color))
        phase_bar.setPen(QPen(Qt.NoPen))

        # Label
        label = QGraphicsSimpleTextItem(stone.label[:18], self)
        label.setFont(QFont("Consolas", 9, QFont.Bold))
        label.setBrush(QBrush(QColor(TEXT_LIGHT)))
        label_rect = label.boundingRect()
        label.setPos(
            x + (NODE_W - label_rect.width()) / 2,
            y + 10,
        )

        # Status text
        status_text = stone.phase.title()
        if stone.hypotheses:
            active_h = [h for h in stone.hypotheses if h.status == "active"]
            if active_h:
                top = max(active_h, key=lambda h: h.confidence)
                status_text = f"{stone.phase.title()} | {top.confidence:.0%}"

        status = QGraphicsSimpleTextItem(status_text, self)
        status.setFont(QFont("Consolas", 7))
        status.setBrush(QBrush(QColor(TEXT_DIM)))
        sr = status.boundingRect()
        status.setPos(x + (NODE_W - sr.width()) / 2, y + 30)

    @property
    def center(self) -> QPointF:
        r = self.rect()
        return QPointF(r.x() + r.width() / 2, r.y() + r.height() / 2)

    @property
    def bottom_center(self) -> QPointF:
        r = self.rect()
        return QPointF(r.x() + r.width() / 2, r.y() + r.height())

    @property
    def top_center(self) -> QPointF:
        r = self.rect()
        return QPointF(r.x() + r.width() / 2, r.y())


class HypothesisItem(QGraphicsRectItem):
    """Visual representation of a Hypothesis."""

    def __init__(self, hyp: Hypothesis, x: float, y: float, parent=None):
        super().__init__(x, y, HYP_W, HYP_H, parent)
        self.hypothesis = hyp

        color = QColor(STATUS_COLORS.get(hyp.status, "#555555"))
        self.setBrush(QBrush(color.darker(250)))
        self.setPen(QPen(color, 1.5))
        self.setZValue(5)

        # Label + confidence
        text = f"{hyp.label[:14]} {hyp.confidence:.0%}"
        label = QGraphicsSimpleTextItem(text, self)
        label.setFont(QFont("Consolas", 7))
        label.setBrush(QBrush(QColor(TEXT_LIGHT)))
        lr = label.boundingRect()
        label.setPos(x + (HYP_W - lr.width()) / 2, y + 4)

        # Attempt dots
        for i, att in enumerate(hyp.attempts):
            att_color = {"success": "#22cc44", "fail": "#cc2222", "partial": "#ccaa00"}.get(att.result, "#555555")
            dot = QGraphicsEllipseItem(
                x + 8 + i * (ATT_R * 2 + 4),
                y + HYP_H - ATT_R * 2 - 4,
                ATT_R * 2,
                ATT_R * 2,
                self,
            )
            dot.setBrush(QBrush(QColor(att_color)))
            dot.setPen(QPen(Qt.NoPen))

    @property
    def top_center(self) -> QPointF:
        r = self.rect()
        return QPointF(r.x() + r.width() / 2, r.y())


# ---------------------------------------------------------------------------
# Tree Graph View
# ---------------------------------------------------------------------------

class AttackTreeScene(QGraphicsScene):
    """Scene that lays out the Treadstone tree as a top-down graph."""

    node_clicked = Signal(str, str)  # (node_type, node_id)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBackgroundBrush(QBrush(QColor(SCENE_BG)))
        self._tree: TreadstoneTree | None = None
        self._stone_items: dict[str, StoneNodeItem] = {}
        self._hyp_items: dict[str, HypothesisItem] = {}

    def set_tree(self, tree: TreadstoneTree) -> None:
        """Load a tree and render it."""
        self._tree = tree
        self._rebuild()

    def _rebuild(self) -> None:
        """Clear and re-render the full tree."""
        self.clear()
        self._stone_items.clear()
        self._hyp_items.clear()

        if not self._tree or not self._tree.stones:
            return

        # Build layout: root stones at top, children below
        # Find roots (no parent_id)
        roots = [s for s in self._tree.stones if s.parent_id is None]
        if not roots:
            roots = self._tree.stones[:1]

        # Assign positions: BFS from roots
        positions: dict[str, tuple[float, float]] = {}
        self._layout_subtree(roots, positions, 0, 0)

        # Draw edges first (behind nodes)
        for stone in self._tree.stones:
            if stone.parent_id and stone.parent_id in positions and stone.id in positions:
                px, py = positions[stone.parent_id]
                cx, cy = positions[stone.id]
                self._draw_edge(
                    QPointF(px + NODE_W / 2, py + NODE_H),
                    QPointF(cx + NODE_W / 2, cy),
                )

        # Draw stone nodes
        for stone in self._tree.stones:
            if stone.id not in positions:
                continue
            x, y = positions[stone.id]
            item = StoneNodeItem(stone, x, y)
            self.addItem(item)
            self._stone_items[stone.id] = item

            # Draw hypotheses below stone
            active_hyps = stone.hypotheses
            if active_hyps:
                total_w = len(active_hyps) * (HYP_W + 20) - 20
                start_x = x + (NODE_W - total_w) / 2
                hy = y + NODE_H + 30

                for i, hyp in enumerate(active_hyps):
                    hx = start_x + i * (HYP_W + 20)
                    hyp_item = HypothesisItem(hyp, hx, hy)
                    self.addItem(hyp_item)
                    self._hyp_items[hyp.id] = hyp_item

                    # Edge from stone to hypothesis
                    self._draw_edge(
                        QPointF(x + NODE_W / 2, y + NODE_H),
                        hyp_item.top_center,
                        dashed=True,
                    )

        # Fit scene rect with padding
        r = self.itemsBoundingRect()
        self.setSceneRect(r.adjusted(-50, -50, 50, 50))

    def _layout_subtree(
        self,
        stones: list[StoneNode],
        positions: dict[str, tuple[float, float]],
        depth: int,
        x_offset: float,
    ) -> float:
        """Recursively layout stones. Returns total width used."""
        if not stones:
            return 0

        y = depth * (V_SPACING + NODE_H + 80)  # extra space for hypotheses
        total_width = 0

        for i, stone in enumerate(stones):
            # Find children
            children = [s for s in (self._tree.stones if self._tree else [])
                       if s.parent_id == stone.id]

            if children:
                # Layout children first to get width
                child_width = self._layout_subtree(
                    children, positions, depth + 1, x_offset + total_width
                )
                # Center parent over children
                x = x_offset + total_width + max(0, (child_width - NODE_W) / 2)
                total_width += max(child_width, NODE_W + H_SPACING)
            else:
                x = x_offset + total_width
                total_width += NODE_W + H_SPACING

            positions[stone.id] = (x, y)

        return total_width

    def _draw_edge(self, start: QPointF, end: QPointF, dashed: bool = False) -> None:
        """Draw a curved edge between two points."""
        path = QPainterPath()
        path.moveTo(start)

        # Bezier control points for a smooth curve
        mid_y = (start.y() + end.y()) / 2
        path.cubicTo(
            QPointF(start.x(), mid_y),
            QPointF(end.x(), mid_y),
            end,
        )

        item = QGraphicsPathItem(path)
        pen = QPen(QColor(TEXT_DIM), 1.5)
        if dashed:
            pen.setStyle(Qt.DashLine)
        item.setPen(pen)
        item.setZValue(1)
        self.addItem(item)

    def mousePressEvent(self, event):
        item = self.itemAt(event.scenePos(), self.views()[0].transform() if self.views() else __import__('PySide6.QtGui', fromlist=['QTransform']).QTransform())
        if isinstance(item, StoneNodeItem):
            self.node_clicked.emit("stone", item.stone.id)
        elif isinstance(item, HypothesisItem):
            self.node_clicked.emit("hypothesis", item.hypothesis.id)
        # Check parents for compound items
        while item and item.parentItem():
            item = item.parentItem()
            if isinstance(item, StoneNodeItem):
                self.node_clicked.emit("stone", item.stone.id)
                break
            elif isinstance(item, HypothesisItem):
                self.node_clicked.emit("hypothesis", item.hypothesis.id)
                break
        super().mousePressEvent(event)


class AttackTreeView(QGraphicsView):
    """Zoomable, pannable view of the attack tree."""

    def __init__(self, scene: AttackTreeScene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHint(QPainter.Antialiasing)
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.setStyleSheet(f"background: {SCENE_BG}; border: none;")
        self._zoom = 1.0

    def wheelEvent(self, event: QWheelEvent) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self._zoom *= factor
        self._zoom = max(0.2, min(3.0, self._zoom))
        self.setTransform(__import__('PySide6.QtGui', fromlist=['QTransform']).QTransform().scale(self._zoom, self._zoom))

    def fit_tree(self) -> None:
        """Fit the entire tree in view."""
        self.fitInView(self.scene().sceneRect(), Qt.KeepAspectRatio)
        self._zoom = self.transform().m11()


# ---------------------------------------------------------------------------
# Detail Panel (right side of splitter)
# ---------------------------------------------------------------------------

class NodeDetailPanel(QWidget):
    """Shows details of a selected tree node — sheets, hypotheses, KT analysis."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._title = QLabel("Select a node")
        self._title.setFont(QFont("Consolas", 11, QFont.Bold))
        self._title.setStyleSheet(f"color: {ORANGE};")
        layout.addWidget(self._title)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 10px;")
        layout.addWidget(self._status_label)

        # Hypothesis confidence bars
        self._confidence_widget = QWidget()
        self._confidence_layout = QVBoxLayout(self._confidence_widget)
        self._confidence_layout.setContentsMargins(0, 0, 0, 0)
        self._confidence_layout.setSpacing(2)
        layout.addWidget(self._confidence_widget)

        # KT Analysis display
        self._kt_label = QLabel("KT Analysis")
        self._kt_label.setStyleSheet(f"color: {TEXT_LIGHT}; font-weight: bold; font-size: 10px;")
        self._kt_label.setVisible(False)
        layout.addWidget(self._kt_label)

        self._kt_text = QPlainTextEdit()
        self._kt_text.setReadOnly(True)
        self._kt_text.setMaximumHeight(150)
        self._kt_text.setStyleSheet(f"""
            QPlainTextEdit {{
                background: #1a1a1a; color: {TEXT_LIGHT};
                border: 1px solid #333; font-family: Consolas; font-size: 9px;
            }}
        """)
        self._kt_text.setVisible(False)
        layout.addWidget(self._kt_text)

        # Attempts list
        self._attempts_label = QLabel("Attempts")
        self._attempts_label.setStyleSheet(f"color: {TEXT_LIGHT}; font-weight: bold; font-size: 10px;")
        self._attempts_label.setVisible(False)
        layout.addWidget(self._attempts_label)

        self._attempts_text = QPlainTextEdit()
        self._attempts_text.setReadOnly(True)
        self._attempts_text.setMaximumHeight(200)
        self._attempts_text.setStyleSheet(f"""
            QPlainTextEdit {{
                background: #1a1a1a; color: {TEXT_LIGHT};
                border: 1px solid #333; font-family: Consolas; font-size: 9px;
            }}
        """)
        self._attempts_text.setVisible(False)
        layout.addWidget(self._attempts_text)

        # Circuit breaker alerts
        self._alerts_label = QLabel("")
        self._alerts_label.setStyleSheet("color: #cc2222; font-size: 10px; font-weight: bold;")
        self._alerts_label.setWordWrap(True)
        layout.addWidget(self._alerts_label)

        layout.addStretch()

    def show_stone(self, stone: StoneNode, cb: CircuitBreaker) -> None:
        """Display stone details."""
        self._title.setText(stone.label)
        self._status_label.setText(
            f"Status: {stone.status} | Phase: {stone.phase} | "
            f"Hypotheses: {len(stone.hypotheses)}"
        )

        # Clear confidence bars
        while self._confidence_layout.count():
            w = self._confidence_layout.takeAt(0).widget()
            if w:
                w.deleteLater()

        # Show hypothesis confidence bars
        for hyp in stone.hypotheses:
            bar = self._make_confidence_bar(hyp)
            self._confidence_layout.addWidget(bar)

        self._kt_label.setVisible(False)
        self._kt_text.setVisible(False)
        self._attempts_label.setVisible(False)
        self._attempts_text.setVisible(False)
        self._alerts_label.setText("")

        # Show circuit breaker alerts
        from openkeel.core.treadstone import check_circuit_breaker
        all_alerts = []
        for hyp in stone.hypotheses:
            if hyp.status == "active":
                all_alerts.extend(check_circuit_breaker(stone, hyp, cb))
        if all_alerts:
            self._alerts_label.setText("\n".join(all_alerts))

    def show_hypothesis(self, hyp: Hypothesis, cb: CircuitBreaker) -> None:
        """Display hypothesis details with KT analysis and attempts."""
        self._title.setText(f"H: {hyp.label}")
        self._status_label.setText(
            f"Confidence: {hyp.confidence:.0%} | Status: {hyp.status} | "
            f"Attempts: {hyp.attempt_count}"
        )

        # Clear confidence
        while self._confidence_layout.count():
            w = self._confidence_layout.takeAt(0).widget()
            if w:
                w.deleteLater()

        single_bar = self._make_confidence_bar(hyp)
        self._confidence_layout.addWidget(single_bar)

        # KT Analysis
        kt = hyp.kt
        if kt.is_observed or kt.is_not_observed or kt.probable_cause:
            self._kt_label.setVisible(True)
            self._kt_text.setVisible(True)
            lines = []
            if kt.is_observed:
                lines.append("IS:")
                lines.extend(f"  + {x}" for x in kt.is_observed)
            if kt.is_not_observed:
                lines.append("ISN'T:")
                lines.extend(f"  - {x}" for x in kt.is_not_observed)
            if kt.distinctions:
                lines.append("DISTINCT:")
                lines.extend(f"  * {x}" for x in kt.distinctions)
            if kt.probable_cause:
                lines.append(f"CAUSE: {kt.probable_cause}")
            if kt.contradictions:
                lines.append("!! CONTRADICTIONS:")
                lines.extend(f"  !! {x}" for x in kt.contradictions)
            self._kt_text.setPlainText("\n".join(lines))
        else:
            self._kt_label.setVisible(False)
            self._kt_text.setVisible(False)

        # Attempts
        if hyp.attempts:
            self._attempts_label.setVisible(True)
            self._attempts_text.setVisible(True)
            lines = []
            for i, att in enumerate(hyp.attempts):
                icon = {"success": "+", "fail": "x", "partial": "~"}.get(att.result, "?")
                lines.append(f"[{icon}] #{i+1} {att.timestamp}")
                if att.command:
                    lines.append(f"    cmd: {att.command[:60]}")
                if att.actual_outcome:
                    lines.append(f"    out: {att.actual_outcome[:80]}")
                if att.notes:
                    lines.append(f"    note: {att.notes[:80]}")
                lines.append("")
            self._attempts_text.setPlainText("\n".join(lines))
        else:
            self._attempts_label.setVisible(False)
            self._attempts_text.setVisible(False)

        # Alerts
        from openkeel.core.treadstone import check_circuit_breaker
        # Need a dummy stone for the check
        alerts = []
        if hyp.attempt_count >= cb.max_attempts_per_hypothesis:
            alerts.append(f"MAX ATTEMPTS ({hyp.attempt_count}/{cb.max_attempts_per_hypothesis})")
        if hyp.confidence <= cb.abandon_threshold:
            alerts.append(f"BELOW THRESHOLD ({hyp.confidence:.0%})")
        if hyp.kt.contradictions:
            alerts.append(f"KT CONTRADICTIONS ({len(hyp.kt.contradictions)})")
        self._alerts_label.setText(" | ".join(alerts) if alerts else "")

    def _make_confidence_bar(self, hyp: Hypothesis) -> QWidget:
        """Create a labeled confidence progress bar widget."""
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        label = QLabel(f"{hyp.label[:12]}")
        label.setFixedWidth(80)
        label.setStyleSheet(f"color: {TEXT_LIGHT}; font-size: 9px;")
        layout.addWidget(label)

        # Bar background
        bar_bg = QWidget()
        bar_bg.setFixedHeight(12)
        bar_bg.setStyleSheet("background: #1a1a1a; border-radius: 3px;")
        bar_bg.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # Fill bar (overlay)
        bar_fill = QWidget(bar_bg)
        pct = max(0, min(1.0, hyp.confidence))
        color = STATUS_COLORS.get(hyp.status, ORANGE)
        bar_fill.setStyleSheet(f"background: {color}; border-radius: 3px;")
        bar_fill.setFixedHeight(12)
        # Width will be set after layout
        bar_fill.setFixedWidth(max(1, int(pct * 100)))

        layout.addWidget(bar_bg)

        pct_label = QLabel(f"{hyp.confidence:.0%}")
        pct_label.setFixedWidth(35)
        pct_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 9px;")
        layout.addWidget(pct_label)

        return w


# ---------------------------------------------------------------------------
# Main Attack Tree Panel Widget
# ---------------------------------------------------------------------------

class AttackTreePanel(QWidget):
    """Full attack tree panel: tree view + detail panel in a vertical splitter."""

    status_changed = Signal(str)  # emits status line text

    def __init__(self, accent: str = ORANGE, parent=None):
        super().__init__(parent)
        self._accent = accent
        self._tree: TreadstoneTree | None = None
        self._mission_dir: Path | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header = QWidget()
        header.setStyleSheet(f"background: #1a1a1a; border-bottom: 1px solid {accent};")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 4, 8, 4)

        title = QLabel("TREADSTONE")
        title.setFont(QFont("Consolas", 10, QFont.Bold))
        title.setStyleSheet(f"color: {accent};")
        header_layout.addWidget(title)

        header_layout.addStretch()

        self._fit_btn = QPushButton("FIT")
        self._fit_btn.setFixedSize(40, 22)
        self._fit_btn.setStyleSheet(f"""
            QPushButton {{ background: #333; color: {TEXT_LIGHT}; border: none; border-radius: 3px; font-size: 9px; }}
            QPushButton:hover {{ background: {accent}; color: {DARK_BG}; }}
        """)
        self._fit_btn.clicked.connect(self._fit_view)
        header_layout.addWidget(self._fit_btn)

        layout.addWidget(header)

        # Splitter: tree view (top) + detail panel (bottom)
        splitter = QSplitter(Qt.Vertical)
        splitter.setStyleSheet(f"""
            QSplitter::handle {{ background: #333; height: 3px; }}
        """)

        self._scene = AttackTreeScene()
        self._scene.node_clicked.connect(self._on_node_clicked)
        self._view = AttackTreeView(self._scene)
        splitter.addWidget(self._view)

        self._detail = NodeDetailPanel()
        self._detail.setStyleSheet(f"background: {DARK_BG};")
        detail_scroll = QScrollArea()
        detail_scroll.setWidget(self._detail)
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setStyleSheet(f"background: {DARK_BG}; border: none;")
        splitter.addWidget(detail_scroll)

        splitter.setSizes([300, 200])
        layout.addWidget(splitter)

        # Auto-refresh timer
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._auto_refresh)
        self._refresh_timer.start(5000)  # 5s poll

    def load_mission(self, mission_name: str) -> None:
        """Load the treadstone tree for a mission."""
        self._mission_dir = missions_dir() / mission_name
        if not self._mission_dir.exists():
            return
        tree = load_tree(self._mission_dir)
        if tree:
            self._tree = tree
            self._scene.set_tree(tree)
            QTimer.singleShot(100, self._fit_view)
            self.status_changed.emit(tree_status_line(tree))

    def set_tree(self, tree: TreadstoneTree, mission_dir: Path | None = None) -> None:
        """Directly set a tree object."""
        self._tree = tree
        self._mission_dir = mission_dir
        self._scene.set_tree(tree)
        QTimer.singleShot(100, self._fit_view)
        self.status_changed.emit(tree_status_line(tree))

    def _fit_view(self) -> None:
        self._view.fit_tree()

    def _on_node_clicked(self, node_type: str, node_id: str) -> None:
        if not self._tree:
            return

        cb = self._tree.circuit_breaker

        if node_type == "stone":
            for stone in self._tree.stones:
                if stone.id == node_id:
                    self._detail.show_stone(stone, cb)
                    break
        elif node_type == "hypothesis":
            for stone in self._tree.stones:
                for hyp in stone.hypotheses:
                    if hyp.id == node_id:
                        self._detail.show_hypothesis(hyp, cb)
                        return

    def _auto_refresh(self) -> None:
        """Reload tree from disk if it changed."""
        if not self._mission_dir:
            return
        tree = load_tree(self._mission_dir)
        if tree and tree.updated_at != (self._tree.updated_at if self._tree else ""):
            self._tree = tree
            self._scene.set_tree(tree)
            self.status_changed.emit(tree_status_line(tree))

    def get_status_line(self) -> str:
        """Get current status line text."""
        if self._tree:
            return tree_status_line(self._tree)
        return ""
