"""Typed vehicle animation metadata and the verified scalar/constraint subset."""

import ast
from dataclasses import dataclass, field
from enum import IntEnum
import math
import operator
import struct

from .coords import gbx_to_blender_vec3
from .errors import ParseError
from .model.common import Vec3


class AnimationOperator(IntEnum):
    SET = 0
    ADD = 1
    SUB = 2
    MULT = 3


class AxisWorkspace(IntEnum):
    GLOBAL = 0
    PARENT = 1
    LOCAL = 2


@dataclass
class JointOperation:
    kind: int
    joint: str
    expression: str = ""
    axis: Vec3 = field(default_factory=Vec3)
    operator: AnimationOperator = AnimationOperator.ADD
    workspace: AxisWorkspace = AxisWorkspace.LOCAL
    target: str = ""
    constraint_a: str = ""
    constraint_b: str = ""
    node_index: int = -1


@dataclass
class AnimationGraph:
    name: str
    node_types: tuple[int, ...]
    connections: tuple[tuple[int, ...], ...]
    defaults: dict[str, float]
    expressions: dict[str, str]
    operations: list[JointOperation]
    children: list["AnimationGraph"]

    def active_operations(self) -> list[JointOperation]:
        reached = {i for i, kind in enumerate(self.node_types) if kind == 26}
        pending = list(reached)
        edges = {}
        for source, _source_port, target, _target_port in self.connections:
            edges.setdefault(source, []).append(target)
        while pending:
            for target in edges.get(pending.pop(), ()):
                if target not in reached:
                    reached.add(target)
                    pending.append(target)
        return [op for op in self.operations if op.node_index in reached]

    def find(self, name: str) -> "AnimationGraph":
        if self.name == name:
            return self
        for child in self.children:
            if child.name == name:
                return child
        raise ValueError(f"Vehicle animation graph unavailable: {name}")


@dataclass
class VehicleAnimation:
    source_path: str
    version: int
    graph_version: int
    variable_version: int
    context_class_ids: tuple[int, ...]
    graphs: list[AnimationGraph]


def decode_vehicle_animation(parsed, source_path: str) -> VehicleAnimation:
    if parsed.classId != 0x090B0000 or parsed._errors:
        raise ParseError(f"Vehicle animation could not be decoded: {source_path}: {parsed._errors}")
    chunk = parsed.body.get(0x090B0003)
    if chunk is None:
        raise ParseError(f"Vehicle animation has no supported graph: {source_path}")

    def graph(raw) -> AnimationGraph:
        operations, children, expressions = [], [], {}
        for index, node in enumerate(raw.children):
            data = node.data
            if node.type == 13:
                children.append(graph(data))
            elif node.type == 20:
                expressions[str(data.name)] = str(data.expression)
            elif node.type in (1, 3):
                operations.append(JointOperation(
                    node.type, str(data.joint), str(data.expression),
                    gbx_to_blender_vec3(data.axis.x, data.axis.y, data.axis.z),
                    AnimationOperator(data.operator), AxisWorkspace(data.axisWorkspace)))
            elif node.type == 2:
                operations.append(JointOperation(
                    node.type, str(data.joint), str(data.rotationExpression), target=str(data.target)))
            elif node.type == 6:
                operations.append(JointOperation(
                    node.type, str(data.joint), constraint_a=str(data.constraintA),
                    constraint_b=str(data.constraintB)))
            if node.type in (1, 2, 3, 6):
                operations[-1].node_index = index
        return AnimationGraph(
            str(raw.base.name), tuple(n.type for n in raw.children),
            tuple(tuple(c) for c in raw.connections),
            {str(v.name): float(v.value) for v in raw.variables if v.type in (0, 1, 2)},
            expressions, operations, children)

    return VehicleAnimation(source_path, chunk.version, chunk.graphVersion, chunk.variableVersion,
                            tuple(chunk.classIds), [graph(g) for g in chunk.graphs])


class ScalarExpression:
    """Arithmetic-only expressions; no attributes, indexing or arbitrary calls."""

    def __init__(self, text: str):
        self.text = text.strip()
        try:
            self.tree = ast.parse(self.text, mode="eval").body
        except SyntaxError as exc:
            raise ValueError(f"Unsupported vehicle expression: {self.text}") from exc
        allowed = (ast.Expression, ast.Constant, ast.Name, ast.Load, ast.BinOp,
                   ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub,
                   ast.UAdd, ast.Call)
        for node in ast.walk(self.tree):
            if not isinstance(node, allowed):
                raise ValueError(f"Unsupported vehicle expression: {self.text}")
            if isinstance(node, ast.Constant) and (type(node.value) not in (int, float)):
                raise ValueError(f"Non-numeric vehicle expression: {self.text}")
            if isinstance(node, ast.Call) and (
                    not isinstance(node.func, ast.Name) or node.func.id not in ("clamp", "min", "max", "abs")
                    or node.keywords):
                raise ValueError(f"Unsupported vehicle function: {self.text}")
        called = {id(node.func) for node in ast.walk(self.tree) if isinstance(node, ast.Call)}
        self.inputs = {node.id for node in ast.walk(self.tree)
                       if isinstance(node, ast.Name) and id(node) not in called}

    def evaluate(self, values: dict[str, float]) -> float:
        missing = self.inputs - values.keys()
        if missing:
            raise ValueError(f"Vehicle expression needs inputs: {', '.join(sorted(missing))}")
        if any(not math.isfinite(values[name]) for name in self.inputs):
            raise ValueError(f"Non-finite vehicle expression input: {self.text}")
        binary = {ast.Add: operator.add, ast.Sub: operator.sub,
                  ast.Mult: operator.mul, ast.Div: operator.truediv}

        def visit(node):
            if isinstance(node, ast.Constant):
                return float(node.value)
            if isinstance(node, ast.Name):
                return values[node.id]
            if isinstance(node, ast.UnaryOp):
                value = visit(node.operand)
                return -value if isinstance(node.op, ast.USub) else value
            if isinstance(node, ast.BinOp):
                return binary[type(node.op)](visit(node.left), visit(node.right))
            if isinstance(node, ast.Call):
                args = [visit(arg) for arg in node.args]
                name = node.func.id
                if name == "clamp" and len(args) == 3 and args[1] <= args[2]:
                    return min(max(args[0], args[1]), args[2])
                if name == "abs" and len(args) == 1:
                    return abs(args[0])
                if name in ("min", "max") and args:
                    return (min if name == "min" else max)(args)
                raise ValueError(f"Invalid vehicle function arguments: {self.text}")
            raise ValueError(f"Unsupported vehicle expression: {self.text}")

        result = visit(self.tree)
        if not math.isfinite(result):
            raise ValueError(f"Non-finite vehicle expression: {self.text}")
        return result


def distance_constraint_delta(a: Vec3, b: Vec3, reference_length: float) -> Vec3:
    """Game JointTranslateDistConstraint: move along A-B by rest minus length."""
    vector = (a.x - b.x, a.y - b.y, a.z - b.z)
    squared = sum(v * v for v in vector)
    if not math.isfinite(squared) or not math.isfinite(reference_length) or reference_length < 0:
        raise ValueError("Invalid vehicle distance constraint")
    if squared <= 1e-10:
        raise ValueError("Degenerate vehicle distance constraint")
    length = math.sqrt(squared)
    scale = (reference_length - length) / length
    return Vec3(*(v * scale for v in vector))


def _float32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def angle_follow(current: float, target: float, step: float, rate: float) -> float:
    """Verified game opcode 0x59; rate is degrees, with float32 arithmetic."""
    if not all(math.isfinite(v) for v in (current, target, step, rate)) or step < 0 or rate < 0:
        raise ValueError("Invalid angle_follow inputs")
    current, target, step, rate = map(_float32, (current, target, step, rate))
    pi = _float32(math.pi)
    negative_pi = -pi
    tau = _float32(math.tau)
    delta = _float32(target - current)
    if delta > 0:
        other = _float32(_float32(_float32(pi - target) + current) - negative_pi)
        if delta > other:
            delta = -other
    else:
        other = _float32(_float32(_float32(target - negative_pi) + pi) - current)
        if -delta > other:
            delta = other
    gain = min(max(abs(delta), _float32(0.2)), _float32(1.5))
    movement = _float32(rate * pi)
    movement = _float32(movement / 180)
    movement = _float32(movement * gain)
    movement = _float32(movement * step)
    movement = min(abs(delta), movement)
    result = _float32(current + math.copysign(movement, delta))
    if result < negative_pi or result >= pi:
        result = _float32(result - negative_pi)
        result = _float32(math.fmod(result, tau))
        if result < 0:
            result = _float32(result + tau)
        result = _float32(result - pi)
    return result


def spoiler_states(animation: VehicleAnimation, samples, open_normed: float, fps: float):
    """Replay contacts drive authored spoiler expressions at scene-frame cadence."""
    if not samples or not math.isfinite(fps) or fps <= 0 or not math.isfinite(open_normed):
        raise ValueError("Invalid spoiler animation inputs")
    root = animation.graphs[0]
    graph = root.find("Spoiler")
    expected = {
        "SpoilerOpen": "SpoilerOpenNormed",
        "SpoilerTop": "angle_follow(SpoilerTop,select((ReactorBoostType!='None')&&(IsGroundContact==False),0,sat(SpoilerOpen*2)),Period*100,3.14)",
        "SpoilerTip": "angle_follow(SpoilerTip,select((ReactorBoostType!='None')&&(IsGroundContact==False),0,sat((SpoilerOpen-0.5)*2)),Period*100,3.14)",
    }
    for name, expression in expected.items():
        if "".join(root.expressions.get(name, "").split()) != expression:
            raise ValueError(f"Unsupported authored spoiler expression: {name}")
    tip_expression = "select((ReactorBoostType!='None')&&(IsGroundContact==False),max(SpoilerBotTip-0.001,0),min(SpoilerBotTip+0.001,SpoilerTip*0.06))"
    if "".join(graph.expressions.get("SpoilerBotTip", "").split()) != tip_expression:
        raise ValueError("Unsupported authored spoiler lower-tip expression")
    for sample in samples:
        if not {"reactor_boost_type", "is_ground_contact"} <= sample.telemetry.keys():
            raise ValueError("Spoiler needs recorded reactor type and ground contact")
    if any(b.time_ms <= a.time_ms for a, b in zip(samples, samples[1:])):
        raise ValueError("Spoiler samples must have increasing timestamps")
    top, tip = root.defaults["SpoilerTop"], root.defaults["SpoilerTip"]
    bottom_tip = graph.defaults["SpoilerBotTip"]
    open_normed = _float32(open_normed)
    first, last = samples[0].time_ms, samples[-1].time_ms
    times = [first + index * 1000 / fps for index in range(int((last-first) * fps / 1000) + 1)]
    if times[-1] < last:
        times.append(last)
    result, index, previous = [], 0, first
    for time in times:
        while index + 1 < len(samples) and samples[index + 1].time_ms <= time:
            index += 1
        telemetry = samples[index].telemetry
        folded = telemetry["reactor_boost_type"] != 0 and not telemetry["is_ground_contact"]
        step = _float32(_float32((time - previous) / 1000) * 100)
        top_target = 0 if folded else min(max(_float32(open_normed * 2), 0), 1)
        tip_target = 0 if folded else min(max(_float32(_float32(open_normed - 0.5) * 2), 0), 1)
        top = angle_follow(top, top_target, step, 3.14)
        tip = angle_follow(tip, tip_target, step, 3.14)
        if time > first:
            bottom_tip = (max(_float32(bottom_tip - _float32(0.001)), 0) if folded else
                          min(_float32(bottom_tip + _float32(0.001)), _float32(tip * _float32(0.06))))
        result.append((time, dict(SpoilerTop=top, SpoilerTip=tip, SpoilerBotTip=bottom_tip)))
        previous = time
    return result
