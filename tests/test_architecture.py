"""Keep the code layered: no import cycle, and the package never imports app.py.

The browser interface is a set of ES modules started by static/js/main.js:
their imports form no cycle either, and every module is used.
"""

import ast
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "document_processor"
INTERFACE = ROOT / "static" / "js"
# The module specifier of each static import or re-export, in either quote
# style, with or without a semicolon: import "./a.js", import { b } from
# "./b.js" and export { c } from "./c.js".
JS_IMPORT = re.compile(
    r"""^\s*(?:import\s*["']([^"']+)["']|(?:import|export)\s[^;]*?\bfrom\s*["']([^"']+)["'])""",
    re.M,
)


class ScriptTags(HTMLParser):
    """Collect the attributes of every <script> tag of a page."""

    def __init__(self):
        super().__init__()
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.scripts.append(dict(attrs))


def js_imports(path):
    """Return the module specifiers imported or re-exported by one interface module."""
    return [
        plain or named
        for plain, named in JS_IMPORT.findall(path.read_text(encoding="utf-8"))
    ]


def imported_modules(path):
    """Return the names of app.py and package modules imported by one source file."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                parts = node.module.split(".")
                names.add(
                    parts[1]
                    if parts[0] == PACKAGE.name and len(parts) > 1
                    else parts[0]
                )
                if node.module == PACKAGE.name:
                    names.update(alias.name for alias in node.names)
            elif node.level == 1:
                # "from .x import y" imports module x; "from . import x, y" imports modules x and y.
                names.update(
                    [node.module.split(".")[0]]
                    if node.module
                    else [alias.name for alias in node.names]
                )
    modules = {path.stem for path in PACKAGE.glob("*.py")}
    return {name for name in names if name in modules or name == "app"}


def find_cycle(graph):
    """Return one import cycle of graph (module -> imported modules) as a list, or None."""
    visiting, done = [], set()

    def visit(module):
        if module in done:
            return None
        if module in visiting:
            return visiting[visiting.index(module) :] + [module]
        visiting.append(module)
        for imported in sorted(graph.get(module, ())):
            cycle = visit(imported)
            if cycle:
                return cycle
        visiting.pop()
        done.add(module)
        return None

    for module in sorted(graph):
        cycle = visit(module)
        if cycle:
            return cycle
    return None


class ArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.graph = {
            path.stem: imported_modules(path) for path in PACKAGE.glob("*.py")
        }

    def test_package_never_imports_the_web_app(self):
        offenders = sorted(
            module for module, imports in self.graph.items() if "app" in imports
        )
        self.assertEqual(offenders, [], "document_processor must not depend on app.py")

    def test_package_imports_form_no_cycle(self):
        cycle = find_cycle(
            {module: imports - {"app"} for module, imports in self.graph.items()}
        )
        self.assertIsNone(cycle, "import cycle: " + " -> ".join(cycle or []))


class InterfaceModuleTests(unittest.TestCase):
    def setUp(self):
        self.imports = {path.name: js_imports(path) for path in INTERFACE.glob("*.js")}

    def test_page_loads_the_entry_module(self):
        tags = ScriptTags()
        tags.feed((ROOT / "templates" / "index.html").read_text(encoding="utf-8"))
        modules = [
            script.get("src", "")
            for script in tags.scripts
            if script.get("type") == "module"
        ]
        self.assertEqual(len(modules), 1, "the page must load exactly one module")
        self.assertIn("filename='js/main.js'", modules[0])

    def test_modules_import_existing_sibling_modules(self):
        for module, specifiers in self.imports.items():
            with self.subTest(module=module):
                # Browsers resolve only relative or absolute URLs without an import map.
                self.assertEqual(
                    [name for name in specifiers if not name.startswith("./")], []
                )
                self.assertEqual(
                    {name[2:] for name in specifiers} - set(self.imports),
                    set(),
                    "import of a missing module",
                )

    def test_interface_imports_form_no_cycle_and_every_module_is_used(self):
        graph = {
            module: {name[2:] for name in specifiers}
            for module, specifiers in self.imports.items()
        }
        cycle = find_cycle(graph)
        self.assertIsNone(cycle, "import cycle: " + " -> ".join(cycle or []))
        reachable, pending = set(), ["main.js"]
        while pending:
            module = pending.pop()
            if module not in reachable:
                reachable.add(module)
                pending.extend(graph.get(module, ()))
        self.assertEqual(
            set(graph) - reachable, set(), "modules never imported from main.js"
        )


if __name__ == "__main__":
    unittest.main()
