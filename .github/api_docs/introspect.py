#!/usr/bin/env python3
"""introspect.py — the static, import-free introspection core for ``api_docs``.

Every other module in this package (the HTML site generator, the MCP server, the
CLI) is a *consumer* of the :class:`Index` this module builds. It is the single
source of truth for "what is the public API of dispatch", and it derives that
answer **without importing a single line of the code it documents**.

Why static (AST) and not runtime import? Two project realities make importing the
target packages fragile:

  * Many submodules use bare sibling imports (``import workplan_config``,
    ``from purpose import ...``) that only resolve when the package directory
    itself is on ``sys.path`` — the layout a tool like pdoc/Sphinx does *not*
    reproduce, so import-based introspection chokes on them.
  * ``foundation.agent_sdk`` (and friends) drive the Claude Agent SDK, which can
    hang the interpreter under Python 3.14 (see ``PROJECT.md`` — the 3.14 caveat).

Parsing with :mod:`ast` sidesteps both: it is deterministic, version-independent,
needs none of the project's runtime dependencies installed, and never executes
module-level side effects. The price is that we describe the API *as written* —
signatures, docstrings, and locations come straight from the source text — which
for documentation is exactly what we want.

Public entry points:
  * :func:`build_index` — walk the package roots, return an :class:`Index`.
  * :func:`lookup`      — render pydoc-style help for a dotted symbol path.
  * :func:`find_usage`  — locate references to a symbol across the source tree.
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from pathlib import Path

# The live, documented packages. ``visitor`` is dead graveyard code and
# ``engine``/``src`` are empty husks left by the foundation rename — none are
# part of the public API, so none are documented. Keep this list in sync with
# the canonical tree described in PROJECT.md.
DEFAULT_ROOTS: tuple[str, ...] = ("foundation", "baseworkflow", "websitewf", "agents")


# --------------------------------------------------------------------------- #
# Data model — plain dataclasses describing the documented surface.
# --------------------------------------------------------------------------- #
@dataclass
class Param:
    """A single parameter in a callable signature."""

    name: str
    annotation: str | None = None
    default: str | None = None
    kind: str = "arg"  # posonly | arg | vararg | kwonly | kwarg

    def render(self) -> str:
        prefix = {"vararg": "*", "kwarg": "**"}.get(self.kind, "")
        out = f"{prefix}{self.name}"
        if self.annotation:
            out += f": {self.annotation}"
        if self.default is not None:
            out += f" = {self.default}" if self.annotation else f"={self.default}"
        return out


@dataclass
class Func:
    """A function or method defined in the source."""

    name: str
    qualname: str
    params: list[Param]
    returns: str | None
    docstring: str
    lineno: int
    file: str
    kind: str = "function"  # function | async function | method | async method
    decorators: list[str] = field(default_factory=list)

    @property
    def is_property(self) -> bool:
        return any(d.split(".")[-1] == "property" for d in self.decorators)

    def signature(self) -> str:
        """Render the call signature the way the source declares it.

        Parameters are emitted in order with the PEP 570 ``/`` (positional-only)
        and PEP 3102 ``*`` (keyword-only) separators inserted where the AST says
        they belong.
        """
        parts: list[str] = []
        seen_posonly = any(p.kind == "posonly" for p in self.params)
        emitted_slash = False
        emitted_star = False
        for p in self.params:
            if seen_posonly and not emitted_slash and p.kind != "posonly":
                parts.append("/")
                emitted_slash = True
            if p.kind == "kwonly" and not emitted_star:
                # bare * before the first keyword-only arg, unless a *args
                # already introduced the keyword-only section.
                if not any(x.kind == "vararg" for x in self.params):
                    parts.append("*")
                emitted_star = True
            parts.append(p.render())
        sig = f"{self.name}({', '.join(parts)})"
        if self.returns:
            sig += f" -> {self.returns}"
        return sig


@dataclass
class Klass:
    """A class defined in the source, with its methods."""

    name: str
    qualname: str
    bases: list[str]
    docstring: str
    lineno: int
    file: str
    methods: list[Func] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)


@dataclass
class Module:
    """One ``.py`` file, addressed by its dotted import path."""

    qualname: str
    file: str
    docstring: str
    is_package: bool
    classes: list[Klass] = field(default_factory=list)
    functions: list[Func] = field(default_factory=list)


@dataclass
class Index:
    """The whole documented surface, plus a flat dotted-path symbol table."""

    repo_root: str
    roots: tuple[str, ...]
    modules: dict[str, Module] = field(default_factory=dict)
    # dotted path -> ("module"|"class"|"function"|"method", owning Module qualname)
    symbols: dict[str, tuple[str, str]] = field(default_factory=dict)

    def module_names(self) -> list[str]:
        return sorted(self.modules)

    def packages(self) -> list[str]:
        return sorted({m.split(".")[0] for m in self.modules})


# --------------------------------------------------------------------------- #
# AST helpers
# --------------------------------------------------------------------------- #
def _unparse(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - unparse is total on valid 3.9+ ASTs
        return "..."


def summary(docstring: str) -> str:
    """First non-blank line of a docstring — the one-liner used in indexes."""
    for line in (docstring or "").strip().splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _params(args: ast.arguments) -> list[Param]:
    out: list[Param] = []
    # Defaults align to the trailing positional params (posonly + args).
    positional = list(args.posonlyargs) + list(args.args)
    n_defaults = len(args.defaults)
    default_for = {
        id(positional[len(positional) - n_defaults + i]): args.defaults[i]
        for i in range(n_defaults)
    } if n_defaults else {}

    for a in args.posonlyargs:
        out.append(Param(a.arg, _unparse(a.annotation), _unparse(default_for.get(id(a))), "posonly"))
    for a in args.args:
        out.append(Param(a.arg, _unparse(a.annotation), _unparse(default_for.get(id(a))), "arg"))
    if args.vararg:
        out.append(Param(args.vararg.arg, _unparse(args.vararg.annotation), None, "vararg"))
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        out.append(Param(a.arg, _unparse(a.annotation), _unparse(d), "kwonly"))
    if args.kwarg:
        out.append(Param(args.kwarg.arg, _unparse(args.kwarg.annotation), None, "kwarg"))
    return out


def _decorators(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> list[str]:
    return [d for d in (_unparse(x) for x in node.decorator_list) if d]


def _func(node: ast.FunctionDef | ast.AsyncFunctionDef, qualprefix: str, file: str, *, method: bool) -> Func:
    is_async = isinstance(node, ast.AsyncFunctionDef)
    kind = ("async " if is_async else "") + ("method" if method else "function")
    return Func(
        name=node.name,
        qualname=f"{qualprefix}.{node.name}",
        params=_params(node.args),
        returns=_unparse(node.returns),
        docstring=ast.get_docstring(node) or "",
        lineno=node.lineno,
        file=file,
        kind=kind,
        decorators=_decorators(node),
    )


def _is_public(name: str) -> bool:
    # A leading underscore marks a private/internal name. ``__init__`` and other
    # dunder methods stay (they are part of a class's documented contract).
    return not name.startswith("_") or (name.startswith("__") and name.endswith("__"))


# --------------------------------------------------------------------------- #
# Module discovery
# --------------------------------------------------------------------------- #
def _dotted(path: Path, repo_root: Path) -> tuple[str, bool]:
    rel = path.relative_to(repo_root).with_suffix("")
    parts = list(rel.parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return ".".join(parts), is_package


def _parse_module(path: Path, repo_root: Path, *, include_private: bool) -> Module | None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return None
    qualname, is_package = _dotted(path, repo_root)
    rel_file = str(path.relative_to(repo_root))
    mod = Module(
        qualname=qualname,
        file=rel_file,
        docstring=ast.get_docstring(tree) or "",
        is_package=is_package,
    )
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if include_private or _is_public(node.name):
                mod.functions.append(_func(node, qualname, rel_file, method=False))
        elif isinstance(node, ast.ClassDef):
            if not (include_private or _is_public(node.name)):
                continue
            klass = Klass(
                name=node.name,
                qualname=f"{qualname}.{node.name}",
                bases=[b for b in (_unparse(x) for x in node.bases) if b],
                docstring=ast.get_docstring(node) or "",
                lineno=node.lineno,
                file=rel_file,
                decorators=_decorators(node),
            )
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if include_private or _is_public(sub.name):
                        klass.methods.append(_func(sub, klass.qualname, rel_file, method=True))
            mod.classes.append(klass)
    return mod


def build_index(
    roots: tuple[str, ...] = DEFAULT_ROOTS,
    repo_root: str | os.PathLike[str] | None = None,
    *,
    include_private: bool = False,
    include_tests: bool = False,
) -> Index:
    """Walk ``roots`` under ``repo_root`` and return a populated :class:`Index`.

    ``repo_root`` defaults to the dispatch repository root inferred from this
    file's location. Test modules (``test_*.py``) and ``__pycache__`` are skipped
    unless ``include_tests`` is set; private names are skipped unless
    ``include_private`` is set.
    """
    root = Path(repo_root) if repo_root else repo_root_from_here()
    index = Index(repo_root=str(root), roots=roots)
    for pkg in roots:
        base = root / pkg
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if not include_tests and path.name.startswith("test_"):
                continue
            mod = _parse_module(path, root, include_private=include_private)
            if mod is None:
                continue
            index.modules[mod.qualname] = mod
            index.symbols[mod.qualname] = ("module", mod.qualname)
            for fn in mod.functions:
                index.symbols[fn.qualname] = ("function", mod.qualname)
            for cls in mod.classes:
                index.symbols[cls.qualname] = ("class", mod.qualname)
                for m in cls.methods:
                    index.symbols[m.qualname] = ("method", mod.qualname)
    return index


def repo_root_from_here() -> Path:
    """Locate the repository root from this file's position in the tree."""
    here = Path(__file__).resolve()
    # .github/api_docs/introspect.py -> repo root is two levels above the package.
    return here.parents[2]


# --------------------------------------------------------------------------- #
# Lookup — pydoc-style rendering of one symbol
# --------------------------------------------------------------------------- #
def _find_func(index: Index, dotted: str) -> Func | None:
    for mod in index.modules.values():
        for fn in mod.functions:
            if fn.qualname == dotted:
                return fn
        for cls in mod.classes:
            for m in cls.methods:
                if m.qualname == dotted:
                    return m
    return None


def _find_class(index: Index, dotted: str) -> Klass | None:
    for mod in index.modules.values():
        for cls in mod.classes:
            if cls.qualname == dotted:
                return cls
    return None


def resolve(index: Index, name: str) -> list[str]:
    """Return dotted paths matching ``name`` exactly, or by trailing component.

    Lets a caller pass a fully-qualified path (``foundation.models.chat``) or a
    bare leaf (``chat``) and get back every symbol that matches.
    """
    if name in index.symbols:
        return [name]
    tail = name.split(".")[-1]
    hits = [s for s in index.symbols if s.split(".")[-1] == tail or s.endswith("." + name)]
    return sorted(hits)


def lookup(index: Index, name: str) -> str:
    """Render pydoc-style help for the symbol(s) matching ``name``.

    Resolution accepts a full dotted path or a bare name. If a bare name is
    ambiguous, a disambiguation list is returned instead of a single entry.
    """
    matches = resolve(index, name)
    if not matches:
        return f"No symbol matching {name!r}. Try list_index to browse, or a fully-qualified path."
    if len(matches) > 1 and name not in index.symbols:
        lines = [f"{name!r} is ambiguous — {len(matches)} matches:"]
        for m in matches:
            kind = index.symbols[m][0]
            lines.append(f"  [{kind}] {m}")
        lines.append("\nCall lookup again with one of the fully-qualified paths above.")
        return "\n".join(lines)

    dotted = matches[0]
    kind = index.symbols[dotted][0]
    if kind == "module":
        return _render_module(index.modules[dotted])
    if kind == "class":
        cls = _find_class(index, dotted)
        return _render_class(cls) if cls else f"(class {dotted} vanished)"
    fn = _find_func(index, dotted)
    return _render_func(fn) if fn else f"(callable {dotted} vanished)"


def _render_func(fn: Func) -> str:
    head = f"{fn.kind}  {fn.qualname.rsplit('.', 1)[0]}.{fn.signature()}"
    out = [head, ""]
    if fn.decorators:
        out.append("decorators: " + ", ".join("@" + d for d in fn.decorators))
        out.append("")
    out.append(fn.docstring.rstrip() if fn.docstring else "(no docstring)")
    out.append("")
    out.append(f"defined in {fn.file}:{fn.lineno}")
    return "\n".join(out)


def _render_class(cls: Klass) -> str:
    base = f"({', '.join(cls.bases)})" if cls.bases else ""
    out = [f"class  {cls.qualname}{base}", ""]
    out.append(cls.docstring.rstrip() if cls.docstring else "(no docstring)")
    out.append("")
    if cls.methods:
        out.append("methods:")
        for m in cls.methods:
            tag = " (property)" if m.is_property else ""
            out.append(f"  {m.signature()}{tag}")
            s = summary(m.docstring)
            if s:
                out.append(f"      {s}")
    out.append("")
    out.append(f"defined in {cls.file}:{cls.lineno}")
    return "\n".join(out)


def _render_module(mod: Module) -> str:
    kind = "package" if mod.is_package else "module"
    out = [f"{kind}  {mod.qualname}", ""]
    out.append(mod.docstring.rstrip() if mod.docstring else "(no module docstring)")
    out.append("")
    if mod.classes:
        out.append("classes:")
        for cls in mod.classes:
            out.append(f"  {cls.name} — {summary(cls.docstring) or '(no docstring)'}")
    if mod.functions:
        out.append("functions:")
        for fn in mod.functions:
            out.append(f"  {fn.signature()}")
            s = summary(fn.docstring)
            if s:
                out.append(f"      {s}")
    out.append("")
    out.append(f"defined in {mod.file}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Usage search — where is this symbol referenced?
# --------------------------------------------------------------------------- #
@dataclass
class Usage:
    file: str
    lineno: int
    line: str
    is_definition: bool = False


def find_usage(
    name: str,
    roots: tuple[str, ...] = DEFAULT_ROOTS,
    repo_root: str | os.PathLike[str] | None = None,
    *,
    limit: int = 100,
    include_tests: bool = True,
) -> list[Usage]:
    """Find every reference to the identifier ``name`` across ``roots``.

    Matches on the *final* component of ``name`` (so ``models.chat`` and ``chat``
    both hunt for the name ``chat``) using the AST — comments and string literals
    never produce false hits. Definition sites are flagged. Results are sorted by
    file then line and capped at ``limit``.
    """
    root = Path(repo_root) if repo_root else repo_root_from_here()
    target = name.split(".")[-1]
    found: list[Usage] = []
    for pkg in roots:
        base = root / pkg
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if not include_tests and path.name.startswith("test_"):
                continue
            try:
                src = path.read_text(encoding="utf-8")
                tree = ast.parse(src)
            except (SyntaxError, UnicodeDecodeError):
                continue
            lines = src.splitlines()
            rel = str(path.relative_to(root))
            hits: dict[int, bool] = {}  # lineno -> is_definition
            for node in ast.walk(tree):
                is_def = False
                matched = False
                if isinstance(node, ast.Name) and node.id == target:
                    matched = True
                elif isinstance(node, ast.Attribute) and node.attr == target:
                    matched = True
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == target:
                    matched = is_def = True
                if matched:
                    hits[node.lineno] = hits.get(node.lineno, False) or is_def
            for ln in sorted(hits):
                text = lines[ln - 1].strip() if 0 < ln <= len(lines) else ""
                found.append(Usage(rel, ln, text, hits[ln]))
    found.sort(key=lambda u: (u.file, u.lineno))
    return found[:limit]
