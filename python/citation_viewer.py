"""
citation_viewer.py — turn a stored citation into (a) a temporary URL the
browser can fetch the original document from, and (b) a self-contained
HTML/JS PDF viewer that best-effort highlights the cited passage.

LEX-specific: not part of the generic project-llm-wiki template. Clicking
a citation needs to open the actual source document with the answer's
supporting text visible — not just a file name. Two pieces make that work:

1. GET_PRESIGNED_URL (a builtin Snowflake stage function) hands back a
   temporary, directly-fetchable HTTPS URL for a file sitting in a stage —
   the browser loads the PDF straight from Snowflake, no bytes ever pass
   through the Streamlit backend's own network path.
2. A small hand-rolled PDF.js viewer (loaded from a CDN, entirely
   client-side) renders that URL and searches its text layer for the
   stored HIGHLIGHT_PHRASE, drawing a highlight box over any match it
   finds. This is genuinely best-effort — OCR text doesn't always line up
   character-for-character with the rendered page, and this file only
   handles horizontal, non-rotated text (the overwhelming majority of
   contract scans, but not a guarantee). It has NOT been exercised against
   a live Streamlit-in-Snowflake app from this environment (no browser
   available here to test in) — validate the coordinate math against a
   real contract PDF before relying on it, and keep the "Open in new tab"
   fallback link visible regardless, since it always works.

Whether Streamlit-in-Snowflake's Content-Security-Policy even allows an
embedded `components.v1.html` iframe to load a script from a CDN is also
unverified from this environment — if it's blocked, the viewer's status
line reports the load failure and the "Open in new tab" link (plain
navigation, not a script) still gets the user to the source document.
"""

import json
from typing import Optional

from config import ProjectConfig
from utils.logging_utils import get_logger

logger = get_logger(__name__)

# A specific, older, classic-script (non-ES-module) PDF.js release — newer
# major versions ship ES-module-only builds that don't expose a global
# `pdfjsLib` the way this component's plain <script src> loading assumes.
# Pinned exactly, not a version range, so a CDN-side upgrade can't silently
# switch the loading style out from under this file. NOTE: this session's
# own sandboxed network policy blocks cdnjs.cloudflare.com outright, so
# this exact URL could not be curl-verified from here — that has no
# bearing on the deployed app (the fetch happens client-side, in the
# viewer's own browser, an entirely different network path), but confirm
# it resolves the first time this page is opened for real, and swap the
# version if not.
_PDFJS_VERSION = "2.16.105"
_PDFJS_BASE = f"https://cdnjs.cloudflare.com/ajax/libs/pdf.js/{_PDFJS_VERSION}"


def get_presigned_url(session, project: ProjectConfig, stage_path: str,
                      expiry_secs: int = 3600) -> Optional[str]:
    """stage_path is RAW_DOCUMENTS.STAGE_PATH, e.g.
    'MEDSCOMA.DATA_LEX.DOCS_STAGE/CW12345_Executed.pdf' — strips the leading
    qualified-stage prefix to get the path GET_PRESIGNED_URL expects
    relative to the stage. Returns None (never raises) if the file can't
    be resolved — a broken citation link shouldn't crash the page it's on."""
    qualified_stage = project.qualified_stage
    prefix = f"{qualified_stage}/"
    relative_path = stage_path[len(prefix):] if stage_path.startswith(prefix) else stage_path
    try:
        # The stage reference is inlined as a literal, NOT a bind
        # parameter — this codebase already hit the equivalent gotcha with
        # BUILD_SCOPED_FILE_URL ("stage argument is a bare @stage reference
        # resolved at SQL parse time and can't be a bind parameter at
        # all"), and GET_PRESIGNED_URL is the same vintage of
        # stage-URL-returning function, unlike TO_FILE (which this
        # codebase confirmed DOES accept its stage argument as a bind
        # parameter — a different, newer function). Safe to inline here
        # since qualified_stage comes from ProjectConfig, never user
        # input. Only the relative path and expiry are real bind params.
        row = session.sql(
            f"SELECT GET_PRESIGNED_URL(@{qualified_stage}, ?, ?) AS URL",
            params=[relative_path, expiry_secs],
        ).collect()[0]
        return row["URL"]
    except Exception:
        logger.warning("EVENT=PRESIGNED_URL_FAILED stage_path=%r", stage_path, exc_info=True)
        return None


def is_pdf(file_name: str) -> bool:
    return (file_name or "").lower().endswith(".pdf")


def build_pdf_viewer_html(pdf_url: str, highlight_phrase: Optional[str], height: int = 640) -> str:
    """Returns a full standalone HTML document for st.components.v1.html —
    loads PDF.js from cdnjs, opens pdf_url, finds the page containing
    highlight_phrase (case/whitespace-insensitive substring match across
    that page's text layer), renders that page, and draws a highlight
    rectangle over the matching text. Falls back to page 1 with a status
    message if no phrase is given or none is found — the rendered PDF and
    the "Open in new tab" link are still useful on their own even without
    a match.
    """
    viewer_height = max(height - 40, 300)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  html, body {{ margin:0; padding:0; background:#525659; font-family:sans-serif; }}
  #toolbar {{ background:#2b2f31; color:#eee; font-size:12px; padding:6px 10px;
             display:flex; gap:14px; align-items:center; white-space:nowrap; }}
  #toolbar button {{ cursor:pointer; background:#444; color:#eee; border:1px solid #666;
                     border-radius:3px; padding:2px 8px; }}
  #status {{ color:#ccc; flex:1; overflow:hidden; text-overflow:ellipsis; }}
  #viewer {{ position:relative; width:100%; overflow:auto; height:{viewer_height}px; text-align:center; }}
  #pageContainer {{ position:relative; display:inline-block; margin-top:10px; }}
  canvas {{ display:block; box-shadow:0 0 6px rgba(0,0,0,0.5); }}
  .hl {{ position:absolute; background:rgba(255,235,59,0.55); border-radius:2px; pointer-events:none; }}
  a {{ color:#8ab4f8; }}
</style></head>
<body>
  <div id="toolbar">
    <span id="status">Loading…</span>
    <button id="prev">◀</button>
    <span id="pageInfo"></span>
    <button id="next">▶</button>
    <a href="{pdf_url}" target="_blank" rel="noopener">Open in new tab ↗</a>
  </div>
  <div id="viewer"><div id="pageContainer"></div></div>

<script src="{_PDFJS_BASE}/pdf.min.js"></script>
<script>
(function(){{
  var PDF_URL = {json.dumps(pdf_url)};
  var PHRASE = {json.dumps(highlight_phrase or "")};
  if (typeof pdfjsLib === "undefined") {{
    document.getElementById("status").textContent =
      "PDF preview script didn't load (likely blocked by policy) — use \\"Open in new tab\\" instead.";
  }} else {{
    pdfjsLib.GlobalWorkerOptions.workerSrc = "{_PDFJS_BASE}/pdf.worker.min.js";

    var statusEl = document.getElementById("status");
    var pageInfoEl = document.getElementById("pageInfo");
    var container = document.getElementById("pageContainer");
    var pdfDoc = null, currentPage = 1, matchPage = null;

    function normalize(s) {{ return (s || "").replace(/\\s+/g, " ").trim().toLowerCase(); }}

    function findMatchPage() {{
      if (!PHRASE) return Promise.resolve(null);
      var target = normalize(PHRASE);
      var n = 1;
      function tryPage() {{
        if (n > pdfDoc.numPages) return Promise.resolve(null);
        return pdfDoc.getPage(n).then(function(page) {{
          return page.getTextContent();
        }}).then(function(tc) {{
          var full = normalize(tc.items.map(function(i){{ return i.str; }}).join(" "));
          if (full.indexOf(target) !== -1) return n;
          n += 1;
          return tryPage();
        }});
      }}
      return tryPage();
    }}

    function renderPage(num) {{
      container.innerHTML = "";
      return pdfDoc.getPage(num).then(function(page) {{
        var viewport = page.getViewport({{ scale: 1.4 }});
        var canvas = document.createElement("canvas");
        canvas.width = viewport.width;
        canvas.height = viewport.height;
        container.appendChild(canvas);
        var ctx = canvas.getContext("2d");
        return page.render({{ canvasContext: ctx, viewport: viewport }}).promise.then(function() {{
          pageInfoEl.textContent = "Page " + num + " of " + pdfDoc.numPages;
          if (!PHRASE || num !== matchPage) return;
          return page.getTextContent().then(function(tc) {{
            var target = normalize(PHRASE);
            var full = "", ranges = [];
            tc.items.forEach(function(item, idx) {{
              var start = full.length;
              full += normalize(item.str) + " ";
              ranges.push({{ idx: idx, start: start, end: full.length }});
            }});
            var pos = full.indexOf(target);
            if (pos === -1) return;
            var matchEnd = pos + target.length;
            tc.items.forEach(function(item, idx) {{
              var r = ranges[idx];
              if (r.end <= pos || r.start >= matchEnd) return;
              // Approximate bounding box from PDF.js's text-item transform,
              // composed with the page viewport's transform — see this
              // file's module docstring for the caveats (untested here,
              // axis-aligned text only).
              var tr = pdfjsLib.Util.transform(viewport.transform, item.transform);
              var fontHeight = Math.hypot(tr[2], tr[3]) || 10;
              var w = item.width * Math.hypot(tr[0], tr[1]);
              var x = tr[4];
              var y = tr[5] - fontHeight * 0.85;
              var hl = document.createElement("div");
              hl.className = "hl";
              hl.style.left = x + "px";
              hl.style.top = y + "px";
              hl.style.width = Math.max(w, 4) + "px";
              hl.style.height = (fontHeight * 1.15) + "px";
              container.appendChild(hl);
            }});
          }});
        }});
      }});
    }}

    document.getElementById("prev").onclick = function() {{
      if (currentPage > 1) {{ currentPage -= 1; renderPage(currentPage); }}
    }};
    document.getElementById("next").onclick = function() {{
      if (pdfDoc && currentPage < pdfDoc.numPages) {{ currentPage += 1; renderPage(currentPage); }}
    }};

    pdfjsLib.getDocument(PDF_URL).promise.then(function(pdf) {{
      pdfDoc = pdf;
      statusEl.textContent = PHRASE ? "Searching for the cited passage…" : "Loaded.";
      return findMatchPage();
    }}).then(function(page) {{
      matchPage = page;
      currentPage = matchPage || 1;
      statusEl.textContent = matchPage
        ? ("Match found on page " + matchPage + ".")
        : (PHRASE ? "Could not locate an exact match in the rendered text — showing page 1; try \\"Open in new tab\\" and Ctrl+F for the phrase shown below." : "Loaded.");
      return renderPage(currentPage);
    }}).catch(function(err) {{
      statusEl.textContent = "Could not load the PDF preview (" + err + ") — use \\"Open in new tab\\" instead.";
    }});
  }}
}})();
</script>
</body></html>"""
