"""Real backend implementation for the calculator tool."""

import ast
import operator

# Allowed arithmetic operators mapped to their Python functions
_SAFE_ARITHMETIC_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval_arithmetic(expr: str):
    """Safely evaluate arithmetic expressions using AST parsing.
    
    Only allows numeric constants, binary operators (+, -, *, /),
    unary operators (negation, positive), and parentheses.
    This is a secure alternative to eval() for arithmetic evaluation.
    """
    try:
        tree = ast.parse(expr.strip(), mode='eval')
    except SyntaxError:
        raise ValueError("Invalid arithmetic expression")

    def _eval_node(node):
        if isinstance(node, ast.Expression):
            return _eval_node(node.body)
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError("Unsupported literal type")
        elif isinstance(node, ast.BinOp):
            left = _eval_node(node.left)
            right = _eval_node(node.right)
            op_type = type(node.op)
            if op_type not in _SAFE_ARITHMETIC_OPS:
                raise ValueError(f"Unsupported operator: {op_type.__name__}")
            return _SAFE_ARITHMETIC_OPS[op_type](left, right)
        elif isinstance(node, ast.UnaryOp):
            operand = _eval_node(node.operand)
            op_type = type(node.op)
            if op_type not in _SAFE_ARITHMETIC_OPS:
                raise ValueError(f"Unsupported operator: {op_type.__name__}")
            return _SAFE_ARITHMETIC_OPS[op_type](operand)
        else:
            raise ValueError(f"Unsupported expression: {type(node).__name__}")

    return _eval_node(tree)


def execute(agent, args: dict) -> dict:
    expression = args.get("expression", "")
    allowed_chars = set("0123456789+-*/.() ")
    if not all(c in allowed_chars for c in expression):
        return {"error": "Invalid characters in expression"}
    try:
        result = _safe_eval_arithmetic(expression)
        return {"result": result}
    except Exception as e:
        return {"error": f"Calculation error: {str(e)}"}


def test_execute():
    assert execute({}, {"expression": "2+2"}) == {"result": 4}
    assert execute({}, {"expression": "10 * 5"}) == {"result": 50}
    assert abs(execute({}, {"expression": "10/3"})["result"] - 3.3333) < 0.01
    assert "error" in execute({}, {"expression": "import os"})
    assert "error" in execute({}, {"expression": ""})
