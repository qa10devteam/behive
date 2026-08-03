#!/usr/bin/env python3
"""Auto-generate coverage-gap tests by introspecting uncovered line ranges.
For each uncovered block: identify the function, generate a test that CALLS it.
"""
import ast
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# Target modules with their miss line ranges (from cov report)
TARGETS = {
    "prescout": [(572, 609), (621, 675), (721, 790), (913, 942), (944, 1024), (1025, 1100), (1100, 1200), (1200, 1297)],
    "harvest": [(125, 200), (250, 350), (400, 500), (550, 650), (700, 800)],
    "scout": [(80, 150), (200, 300), (350, 450)],
    "master_intelligence": [(150, 250), (300, 400), (450, 550)],
}


def find_function_at_line(filepath, line_no):
    """Find which function/method contains the given line."""
    with open(filepath) as f:
        tree = ast.parse(f.read())
    
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= line_no <= node.end_lineno:
                # Check if it's inside a class
                for parent in ast.walk(tree):
                    if isinstance(parent, ast.ClassDef):
                        for child in ast.iter_child_nodes(parent):
                            if child is node:
                                return f"{parent.name}.{node.name}", node.args
                return node.name, node.args
    return None, None


def main():
    base = "/home/ubuntu/behive-pkg/src/behive/engine"
    
    for module, ranges in TARGETS.items():
        filepath = os.path.join(base, f"{module}.py")
        if not os.path.exists(filepath):
            continue
        
        print(f"\n{'='*60}")
        print(f"MODULE: {module}.py")
        print(f"{'='*60}")
        
        seen_funcs = set()
        for start, end in ranges:
            func_name, args = find_function_at_line(filepath, start)
            if func_name and func_name not in seen_funcs:
                seen_funcs.add(func_name)
                arg_names = [a.arg for a in args.args if a.arg != 'self'] if args else []
                print(f"  L{start}-{end}: {func_name}({', '.join(arg_names[:4])})")


if __name__ == "__main__":
    main()
