#!/usr/bin/env python3
"""htmlgen.py — render an :class:`~api_docs.introspect.Index` to a static site.

The output is a self-contained directory (no external assets, no build step, no
JavaScript framework) suitable for publishing straight to GitHub Pages:

  index.html        landing page: a per-package, click-to-sort table of modules
  <module>.html     one page per module, listing classes, methods, functions
  search.json       flat symbol table powering the filter box and agent tooling
  apidocs.css       embedded-free stylesheet (light theme, one file, no CDN)
  apidocs.js        live filter + Notion-style click-to-sort tables (vanilla JS)

Docstrings are rendered through a small, dependency-free formatter
(:func:`_render_doc`) that understands the markup these docstrings actually use —
reStructuredText inline literals (``` ``code`` ```), interpreted roles
(``:class:`X```), Markdown ``**bold**`` / ``*italic*``, bullet lists, and indented
/ ``::`` code blocks. No third-party library, so the build stays stdlib-only and
hermetic (the reason CI needs no ``pip install``).
"""
from __future__ import annotations

import html
import json
import re
import textwrap
from pathlib import Path

from .introspect import Index, Klass, Module, build_index, summary

_CSS = """\
:root { --fg:#1f2328; --muted:#59636e; --bg:#ffffff; --card:#f6f8fa; --accent:#0969da;
        --border:#d1d9e0; --hover:#f0f3f6;
        --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
        --sans:"Comic Sans MS","Comic Sans","Chalkboard SE","Comic Neue",cursive; }
* { box-sizing: border-box; }
body { font: 15px/1.65 var(--sans); color: var(--fg); background: var(--bg); margin: 0; }
.wrap { max-width: 1000px; margin: 0 auto; padding: 1.5rem 1.25rem 4rem; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.muted { color: var(--muted); }
code, .sig, pre { font-family: var(--mono); font-size: .85em; }

/* Header — flex column, tight gaps (no <p> margins fighting the layout) */
header { display: flex; flex-direction: column; gap: .35rem;
         border-bottom: 1px solid var(--border); padding-bottom: 1rem; margin-bottom: 1.5rem; }
.crumbs { font-size: .85rem; color: var(--muted); }
.title-row { display: flex; align-items: center; gap: .6rem; flex-wrap: wrap; }
h1 { font-size: 1.7rem; margin: 0; }
.subtitle { color: var(--muted); font-style: italic; font-size: 1.02rem; }
.lede { margin: 0; }
.kind { font-size: .68rem; text-transform: uppercase; letter-spacing: .05em; color: var(--muted);
        border: 1px solid var(--border); border-radius: 999px; padding: .1rem .5rem; background: var(--card); }

h2 { font-size: 1.15rem; margin: 2rem 0 .5rem; }
h3 { font-size: 1rem; margin: 1.4rem 0 .3rem; }

input#filter { width: 100%; padding: .55rem .7rem; font-size: 1rem; font-family: var(--sans);
               border: 1px solid var(--border); border-radius: 8px; background: var(--bg); color: var(--fg);
               margin: .6rem 0 1rem; }

/* Member entries */
.member { border-left: 2px solid var(--border); padding-left: .85rem; margin: 1.1rem 0; }
.sig { background: var(--card); border: 1px solid var(--border); border-radius: 6px;
       padding: .4rem .6rem; display: block; margin: .35rem 0 .2rem; overflow-x: auto; }
.loc { font-size: .8rem; color: var(--muted); }
footer { margin-top: 3rem; font-size: .8rem; color: var(--muted); border-top: 1px solid var(--border); padding-top: 1rem; }

/* Notion-style module table — subtle separators, hover, click-to-sort headers */
table.mods { border-collapse: collapse; width: 100%; margin: .2rem 0 1.6rem; font-size: .95rem; }
table.mods th, table.mods td { text-align: left; padding: .5rem .7rem; border-bottom: 1px solid var(--border);
                               vertical-align: top; }
table.mods thead th { color: var(--muted); font-weight: 600; font-size: .82rem; cursor: pointer;
                      user-select: none; border-bottom: 2px solid var(--border); white-space: nowrap; }
table.mods thead th::after { content: " ↕"; opacity: .25; font-size: .85em; }
table.mods thead th[data-dir="asc"]::after { content: " ↑"; opacity: .9; }
table.mods thead th[data-dir="desc"]::after { content: " ↓"; opacity: .9; }
table.mods thead th:hover { color: var(--fg); }
table.mods tbody tr:hover td { background: var(--hover); }
table.mods td.n { white-space: nowrap; font-family: var(--mono); font-size: .9em; }
table.mods td.k { white-space: nowrap; color: var(--muted); font-size: .85em; }

/* Rendered docstring prose */
.doc { margin: .2rem 0 1rem; }
.doc p { margin: .55rem 0; }
.doc ul { margin: .4rem 0 .8rem; padding-left: 1.4rem; }
.doc li { margin: .2rem 0; }
.doc pre { background: var(--card); border: 1px solid var(--border); border-radius: 6px;
           padding: .6rem .7rem; overflow-x: auto; margin: .6rem 0; }
.doc pre code { background: none; border: 0; padding: 0; }
.doc code { background: var(--card); border: 1px solid var(--border); border-radius: 4px; padding: .03rem .3rem; }
.doc.muted { font-style: italic; }
"""

_JS = """\
// Live filter (homepage modules + module-page members)
const f=document.getElementById('filter');
if(f){f.addEventListener('input',()=>{const q=f.value.toLowerCase();
document.querySelectorAll('[data-name]').forEach(el=>{
el.style.display=el.dataset.name.includes(q)?'':'none';});});}

// Click-to-sort module tables (Notion-style: click a header to toggle asc/desc)
document.querySelectorAll('table.mods').forEach(t=>{
  const tb=t.tBodies[0]; if(!tb) return;
  t.querySelectorAll('thead th').forEach((th,i)=>{
    th.addEventListener('click',()=>{
      const asc=th.getAttribute('data-dir')!=='asc';
      t.querySelectorAll('thead th').forEach(h=>h.removeAttribute('data-dir'));
      th.setAttribute('data-dir',asc?'asc':'desc');
      [...tb.rows].sort((a,b)=>{
        const x=a.cells[i].textContent.trim().toLowerCase();
        const y=b.cells[i].textContent.trim().toLowerCase();
        return asc?x.localeCompare(y):y.localeCompare(x);
      }).forEach(r=>tb.appendChild(r));
    });
  });
});
"""

# --------------------------------------------------------------------------- #
# Docstring rendering (stdlib-only markdown/RST subset)
# --------------------------------------------------------------------------- #
_ROLE_RE = re.compile(r":[a-zA-Z][a-zA-Z:]*:`([^`]+)`")   # :class:`X`, :meth:`Y` -> code
_DBL_RE = re.compile(r"``([^`]+)``")                       # ``literal`` -> code
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")                  # **bold**
_SGL_RE = re.compile(r"`([^`]+)`")                         # `literal` -> code
_EM_RE = re.compile(r"\*(?=\S)([^*]+?)(?<=\S)\*")          # *italic*
_BULLET_RE = re.compile(r"^\s*[*\-]\s+\S")


def _inline(escaped: str) -> str:
    """Apply inline formatting to already-HTML-escaped text.

    Order matters: interpreted roles and double-backtick literals are resolved
    before single-backtick code so a ``:role:`x``` is not mistaken for inline
    code, and bold (``**``) before italic (``*``) so the emphasis markers don't
    collide.
    """
    s = _ROLE_RE.sub(r"<code>\1</code>", escaped)
    s = _DBL_RE.sub(r"<code>\1</code>", s)
    s = _BOLD_RE.sub(r"<strong>\1</strong>", s)
    s = _SGL_RE.sub(r"<code>\1</code>", s)
    s = _EM_RE.sub(r"<em>\1</em>", s)
    # Balanced code/emphasis is now converted; any backticks left are unbalanced
    # source artifacts (malformed RST). Drop them so none leak to the reader.
    s = s.replace("`", "")
    return s


def _fmt(raw: str) -> str:
    """Escape then inline-format a one-line fragment (e.g. a table summary)."""
    return _inline(html.escape(raw))


def _split_paragraphs(doc: str) -> list[list[str]]:
    """Split a docstring into blank-line-separated blocks of lines."""
    blocks: list[list[str]] = []
    cur: list[str] = []
    for line in (doc or "").rstrip().split("\n"):
        if line.strip() == "":
            if cur:
                blocks.append(cur)
                cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append(cur)
    return blocks


def _render_list(lines: list[str]) -> str:
    items: list[str] = []
    for ln in lines:
        if _BULLET_RE.match(ln):
            items.append(re.sub(r"^\s*[*\-]\s+", "", ln).strip())
        elif items:  # continuation line of the current bullet
            items[-1] += " " + ln.strip()
    return "<ul>" + "".join(f"<li>{_inline(html.escape(it))}</li>" for it in items) + "</ul>"


def _render_paragraphs(blocks: list[list[str]]) -> str:
    out: list[str] = []
    for blk in blocks:
        if any(_BULLET_RE.match(ln) for ln in blk):
            out.append(_render_list(blk))
        elif all((len(ln) - len(ln.lstrip())) >= 4 for ln in blk):
            code = textwrap.dedent("\n".join(blk)).rstrip()
            out.append(f"<pre><code>{html.escape(code)}</code></pre>")
        else:
            joined = re.sub(r" *::$", ":", " ".join(ln.strip() for ln in blk).rstrip())
            out.append(f"<p>{_inline(html.escape(joined))}</p>")
    return "\n".join(out)


def _render_doc(doc: str) -> str:
    """Render a docstring to block-level HTML (paragraphs, lists, code blocks)."""
    if not doc or not doc.strip():
        return ""
    return _render_paragraphs(_split_paragraphs(doc))


def _cap(text: str) -> str:
    """Capitalize the first letter of a prose summary (skip code/acronym starts)."""
    if text and text[0].islower() and (len(text) < 2 or not text[1].isupper()):
        return text[0].upper() + text[1:]
    return text


def _doc_block(doc: str) -> str:
    rendered = _render_doc(doc)
    if rendered:
        return f'<div class="doc">{rendered}</div>'
    return '<div class="doc muted">(no docstring)</div>'


def _strip_lead(text: str) -> str:
    """Drop the repo's redundant ``name —`` docstring-summary prefix.

    These docstrings open with ``<file-or-module> — <description>``; on the index
    the name is already the row label, so the leading token is noise. Only strip
    when the part before the first em-dash is short (a name, not a sentence that
    happens to contain an em-dash).
    """
    parts = re.split(r"\s+—\s+", text, maxsplit=1)
    if len(parts) == 2 and len(parts[0].split()) <= 4:
        return parts[1].strip()
    return text


def _esc(s: str) -> str:
    return html.escape(s or "")


def _page(title: str, body: str) -> str:
    root = "./"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<link rel="stylesheet" href="{root}apidocs.css"></head>
<body><div class="wrap">{body}
<footer>Generated by <code>api_docs</code> — dispatch API reference.
Static, derived from source docstrings. <a href="{root}index.html">Index</a></footer>
</div><script src="{root}apidocs.js"></script></body></html>
"""


def _module_filename(qualname: str) -> str:
    return qualname.replace(".", "-") + ".html"


def _render_member_func(fn) -> str:
    tag = ' <span class="kind">property</span>' if getattr(fn, "is_property", False) else ""
    deco = ""
    if fn.decorators:
        deco = '<div class="loc">' + ", ".join("@" + _esc(d) for d in fn.decorators) + "</div>"
    return (
        f'<div class="member" id="{_esc(fn.name)}" data-name="{_esc(fn.name.lower())}">'
        f'<h3>{_esc(fn.name)}<span class="kind">{_esc(fn.kind)}</span>{tag}</h3>'
        f'{deco}<code class="sig">{_esc(fn.signature())}</code>'
        f'{_doc_block(fn.docstring)}<div class="loc">{_esc(fn.file)}:{fn.lineno}</div></div>'
    )


def _render_member_class(cls: Klass) -> str:
    bases = f"({_esc(', '.join(cls.bases))})" if cls.bases else ""
    methods = "".join(_render_member_func(m) for m in cls.methods)
    inner = f'<div style="margin-left:1rem">{methods}</div>' if methods else ""
    return (
        f'<div class="member" id="{_esc(cls.name)}" data-name="{_esc(cls.name.lower())}">'
        f'<h3>class {_esc(cls.name)}{bases}<span class="kind">class</span></h3>'
        f'{_doc_block(cls.docstring)}<div class="loc">{_esc(cls.file)}:{cls.lineno}</div>{inner}</div>'
    )


def _render_module_page(mod: Module) -> str:
    kind = "package" if mod.is_package else "module"
    # The summary (lead paragraph, minus the redundant "name —" prefix) becomes
    # the header subtitle; the body renders the remaining paragraphs so the
    # description is not repeated.
    paras = _split_paragraphs(mod.docstring)
    subtitle = ""
    body_html = ""
    if paras:
        lead = " ".join(ln.strip() for ln in paras[0])
        sub = _cap(_strip_lead(lead))
        subtitle = f'<div class="subtitle">{_inline(html.escape(sub))}</div>'
        if len(paras) > 1:
            body_html = _render_paragraphs(paras[1:])
    header = (
        '<header><div class="crumbs"><a href="index.html">dispatch API</a> / '
        f"{_esc(mod.qualname)}</div>"
        f'<div class="title-row"><span class="kind">{kind}</span>'
        f"<h1>{_esc(mod.qualname)}</h1></div>{subtitle}</header>"
    )
    body = [header]
    if body_html:
        body.append(f'<div class="doc">{body_html}</div>')
    body.append('<input id="filter" placeholder="filter members in this module…" autocomplete="off">')
    if mod.classes:
        body.append("<h2>Classes</h2>")
        body.extend(_render_member_class(c) for c in mod.classes)
    if mod.functions:
        body.append("<h2>Functions</h2>")
        body.extend(_render_member_func(f) for f in mod.functions)
    if not mod.classes and not mod.functions:
        body.append('<p class="muted">No public classes or functions.</p>')
    return _page(f"{mod.qualname} — dispatch API", "\n".join(body))


def _render_index_page(index: Index) -> str:
    body = [
        '<header><div class="title-row"><h1>dispatch API reference</h1></div>'
        '<div class="subtitle">Auto-generated from source docstrings. '
        f"{len(index.modules)} modules across {len(index.packages())} packages.</div></header>",
        '<input id="filter" placeholder="filter modules…" autocomplete="off">',
    ]
    for pkg in index.packages():
        mods = [m for m in index.module_names() if m == pkg or m.startswith(pkg + ".")]
        body.append(f"<h2>{_esc(pkg)}</h2>")
        body.append('<table class="mods"><thead><tr><th>Module</th><th>Kind</th><th>Summary</th></tr></thead><tbody>')
        for q in mods:
            mod = index.modules[q]
            kind = "package" if mod.is_package else "module"
            desc = _fmt(_strip_lead(summary(mod.docstring)))
            body.append(
                f'<tr data-name="{_esc(q.lower())}">'
                f'<td class="n"><a href="{_module_filename(q)}">{_esc(q)}</a></td>'
                f'<td class="k">{kind}</td>'
                f"<td>{desc}</td></tr>"
            )
        body.append("</tbody></table>")
    return _page("dispatch API reference", "\n".join(body))


def search_records(index: Index) -> list[dict]:
    """Flat symbol table: one record per documented symbol (the search index)."""
    out: list[dict] = []
    for q, mod in index.modules.items():
        page = _module_filename(q)
        out.append({"path": q, "kind": "package" if mod.is_package else "module",
                    "summary": summary(mod.docstring), "url": page, "file": mod.file})
        for fn in mod.functions:
            out.append({"path": fn.qualname, "kind": fn.kind, "summary": summary(fn.docstring),
                        "url": f"{page}#{fn.name}", "file": fn.file})
        for cls in mod.classes:
            out.append({"path": cls.qualname, "kind": "class", "summary": summary(cls.docstring),
                        "url": f"{page}#{cls.name}", "file": cls.file})
            for m in cls.methods:
                out.append({"path": m.qualname, "kind": m.kind, "summary": summary(m.docstring),
                            "url": f"{page}#{m.name}", "file": m.file})
    return out


def build_site(out_dir: str | Path, index: Index | None = None, **index_kwargs) -> Path:
    """Write the full static site to ``out_dir`` and return the path written.

    Builds an :class:`Index` if one is not supplied (passing through any
    ``build_index`` keyword arguments). Existing ``out_dir`` contents are left in
    place except for files this generator overwrites.
    """
    if index is None:
        index = build_index(**index_kwargs)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "apidocs.css").write_text(_CSS, encoding="utf-8")
    (out / "apidocs.js").write_text(_JS, encoding="utf-8")
    (out / "index.html").write_text(_render_index_page(index), encoding="utf-8")
    (out / "search.json").write_text(json.dumps(search_records(index), indent=1), encoding="utf-8")
    # GitHub Pages serves Jekyll by default, which drops files it considers
    # special; .nojekyll publishes the directory verbatim.
    (out / ".nojekyll").write_text("", encoding="utf-8")
    for q, mod in index.modules.items():
        (out / _module_filename(q)).write_text(_render_module_page(mod), encoding="utf-8")
    return out
