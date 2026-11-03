"""Keep the package layered: modules never import app.py and never import each other in a cycle."""

import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "document_processor"


def imported_modules(path):
    """Return the names of app.py and package modules imported by one source file."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                parts = node.module.split(".")
                names.add(parts[1] if parts[0] == PACKAGE.name and len(parts) > 1 else parts[0])
                if node.module == PACKAGE.name:
                    names.update(alias.name for alias in node.names)
            elif node.level == 1:
                # "from .x import y" imports module x; "from . import x, y" imports modules x and y.
                names.update([node.module.split(".")[0]] if node.module else [alias.name for alias in node.names])
    modules = {path.stem for path in PACKAGE.glob("*.py")}
    return {name for name in names if name in modules or name == "app"}


class ArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.graph = {path.stem: imported_modules(path) for path in PACKAGE.glob("*.py")}

    def test_package_never_imports_the_web_app(self):
        offenders = sorted(module for module, imports in self.graph.items() if "app" in imports)
        self.assertEqual(offenders, [], "document_processor must not depend on app.py")

    def test_package_imports_form_no_cycle(self):
        visiting, done = [], set()

        def visit(module):
            if module in done:
                return
            if module in visiting:
                cycle = visiting[visiting.index(module):] + [module]
                self.fail("import cycle: " + " -> ".join(cycle))
            visiting.append(module)
            for imported in sorted(self.graph[module] - {"app"}):
                visit(imported)
            visiting.pop()
            done.add(module)

        for module in sorted(self.graph):
            visit(module)


if __name__ == "__main__":
    unittest.main()
