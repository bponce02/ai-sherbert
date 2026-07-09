# `sherbert_pdf` — PDF annotation backend + Konva editor

`sherbert_pdf` stores annotations against uploaded PDFs, exposes them through a
mountable Django-Ninja router, renders a standalone Konva.js editor, and exports
a flattened annotated PDF with PyMuPDF.

- Install with the extra: `orange-sherbert[pdf]` (adds `pymupdf`, `django-ninja`).
- Add `"sherbert_pdf"` (and `"django.contrib.staticfiles"`) to `INSTALLED_APPS`
  and run `python manage.py migrate sherbert_pdf`.

## Contents

- [Models](#models)
- [Annotation data shapes](#annotation-data-shapes)
- [REST API](#rest-api)
- [Mounting the router](#mounting-the-router)
- [Access control](#access-control)
- [The editor](#the-editor)
- [Stamps](#stamps)
- [Export](#export)
- [Editor capabilities and limitations](#editor-capabilities-and-limitations)

## Models

All models subclass an abstract `BaseModel` (`created_at` auto-add,
`updated_at` auto-now, `is_active` boolean, all nullable) that intentionally
mirrors CoreCRM's `utils.models.BaseModel` column-for-column.

### `PDFDocument`

| Field | Type | Notes |
|---|---|---|
| `title` | `CharField(max_length=255)` | |
| `file` | `FileField(upload_to='pdfs/')` | nullable/blank; requires `MEDIA_ROOT`/`MEDIA_URL`. |
| `user` | `FK('auth.User')` | nullable; the **owner** used by the default access policy. |
| `quick_edit` | `BooleanField(default=False)` | set to `True` for docs created via the upload endpoint. |
| *(+ BaseModel fields)* | | `created_at`, `updated_at`, `is_active`. |

Method: **`export() -> bytes`** — returns the flattened annotated PDF (see
[Export](#export)).

### `PDFAnnotation`

One row per annotation. Type-specific geometry lives in a related `*_data` model.

| Field | Type | Notes |
|---|---|---|
| `pdf_document` | `FK(PDFDocument, related_name='annotations')` | |
| `user` | `FK('auth.User')` | nullable; the annotation's author. |
| `page_number` | `IntegerField` | **0-indexed** (MuPDF convention). |
| `annotation_type` | `CharField(choices)` | `pen`, `highlighter`, `text`, `stamp`, `cloud`. |
| `color_r` / `color_g` / `color_b` | `FloatField` | RGB as floats `0.0–1.0`, nullable. |

Method: **`get_annotation_data() -> dict`** — reconstructs the type-specific
dict from the related data row and the color columns (shapes below).

### Type-data models (one-to-one with `PDFAnnotation`)

**`PenAnnotationData`** (`related_name='pen_data'`) — used by `pen`,
`highlighter`, **and** `cloud`:

| Field | Type | Default |
|---|---|---|
| `vertices` | `JSONField` | — (`[[[x1,y1],[x2,y2],...]]`) |
| `border_width` | `FloatField` | `1.0` |
| `opacity` | `FloatField` | `1.0` |
| `erasures` | `JSONField` | `[]` — eraser circles `[{cx, cy, r}]` in PDF coords |

**`TextAnnotationData`** (`related_name='text_data'`):

| Field | Type | Default |
|---|---|---|
| `rect_x1`,`rect_y1`,`rect_x2`,`rect_y2` | `FloatField` | — |
| `content` | `TextField` | — |
| `font_size` | `IntegerField` | `16` |
| `font_family` | `CharField(255)` | `'Arial, sans-serif'` |
| `font_style` | `CharField(20)` | `'normal'` |

**`StampAnnotationData`** (`related_name='stamp_data'`):

| Field | Type |
|---|---|
| `x`, `y`, `width`, `height` | `FloatField` |
| `image_url` | `CharField(500)` |

## Annotation data shapes

The `annotation_data` object in API payloads is a discriminated union validated
by Pydantic schemas (`sherbert_pdf/schemas.py`). Coordinates are in **PDF
points** in the page coordinate space.

### Pen / Highlighter / Cloud — `PenAnnotationData`

`pen`, `highlighter`, and `cloud` all use this schema on the wire.

```json
{
  "vertices": [[[100.0, 120.0], [140.0, 160.0], [180.0, 150.0]]],
  "colors": { "stroke": [1.0, 0.0, 0.0] },
  "border": { "width": 2.0 },
  "opacity": 1.0,
  "erasures": [{ "cx": 130.0, "cy": 140.0, "r": 8.0 }]
}
```

- `vertices` — `List[List[List[float]]]`: an array of strokes; each stroke is an
  array of `[x, y]` points. (The editor uses a single stroke: `vertices[0]`.)
- `colors.stroke` — exactly 3 floats `0.0–1.0`.
- `border.width` — `>= 0`.
- `opacity` — `0.0–1.0`, default `1.0`. (On export, highlighter opacity is
  forced to `0.3` regardless of this value.)
- `erasures` — optional; list of `{cx, cy, r}` circles. Only meaningful for
  pen/highlighter. See the [export caveat](#export).

**Cloud "new format":** a revision cloud is stored as a **single bounding rect**
`[x, y, w, h]` placed at `vertices[0][0]`, i.e. `vertices == [[[x, y, w, h]]]`
(one stroke, one point, four numbers). The export code detects this
(`len(rect)==1 and len(rect[0])==4`) and generates a scalloped cloud outline
from the rect. A **legacy** cloud (an actual polyline of `[x,y]` points) is still
accepted and drawn as-is.

### Text — `TextAnnotationData`

```json
{
  "rect": [100.0, 200.0, 300.0, 240.0],
  "content": "Reviewed",
  "colors": { "stroke": [0.0, 0.0, 0.0] },
  "fontSize": 16,
  "fontFamily": "Arial, sans-serif",
  "fontStyle": "normal"
}
```

- `rect` — exactly 4 floats `[x1, y1, x2, y2]`.
- `fontSize` — int `>= 1`, default `16`.
- `fontFamily` default `'Arial, sans-serif'`; `fontStyle` default `'normal'`.
- On export, a cursive/script `fontFamily` is rendered italic (signature
  styling). Note: the create endpoint persists `fontFamily`, but the **update**
  endpoint does not re-save `fontFamily` (it updates rect/content/size/style
  only).

### Stamp — `StampAnnotationData`

```json
{
  "type": "stamp",
  "x": 120.0,
  "y": 90.0,
  "width": 160.0,
  "height": 80.0,
  "imageUrl": "/static/sherbert_pdf/stamps/approved.png"
}
```

- `type` — literal `"stamp"`.
- `width`, `height` — floats `> 0`.
- `imageUrl` — the stamp image URL (resolved to a file on export, see below).

## REST API

Endpoints are defined on a `ninja.Router` in `sherbert_pdf/api.py`
(`tags=["Sherbert PDF"]`). Paths below are **relative to the router mount
point** (e.g. `/api` in the example project). The router carries **no auth of
its own** — the host supplies it (see [Mounting](#mounting-the-router)).

| Method | Path | Body | Success | Purpose |
|---|---|---|---|---|
| `POST` | `/pdf-documents` | multipart: `pdf_file` (file), `pdf_title` (query/str, default `"Untitled"`) | `201` `PDFDocumentOut` | Upload a new PDF (sets `user=request.user`, `quick_edit=True`). |
| `GET` | `/pdf-documents/{pdf_id}` | — | `200` `PDFDocumentOut` | Document detail. |
| `GET` | `/pdf-documents/{pdf_id}/export` | — | `200` `application/pdf` | Flattened annotated PDF (inline). |
| `GET` | `/pdf-documents/{pdf_id}/annotations` | — | `200` `list[AnnotationOut]` | All annotations for the document. |
| `POST` | `/annotations` | `AnnotationCreate` (JSON) | `201` `AnnotationOut` | Create an annotation. |
| `PUT` | `/annotations` | `AnnotationUpdate` (JSON) | `204` (no body) | Update an annotation (ID in body). |
| `DELETE` | `/annotations` | `AnnotationDelete` (JSON) | `204` (no body) | Delete an annotation (ID in body). |

> Note: `PUT`/`DELETE` on `/annotations` carry the target `annotation_id` **in
> the JSON body**, not the URL.

### Request/response schemas

```
PDFDocumentOut        = { id: int, title: str, file_url: str }

AnnotationData        = { page_number: int,
                          annotation_type: "pen"|"highlighter"|"text"|"stamp"|"cloud",
                          annotation_data: <PenAnnotationData | TextAnnotationData | StampAnnotationData> }

AnnotationCreate      = { pdf_document_id: int, annotation: AnnotationData }
AnnotationUpdate      = { annotation_id: int, annotation_data: <...union...> }
AnnotationDelete      = { annotation_id: int }

AnnotationOut         = { id: int, page_number: int, annotation_type: str,
                          annotation_data: <...union...>, user_id: int,
                          is_owner: bool, user_name: str }
```

`user_name` in `AnnotationOut` is the author's `get_full_name()` (falls back to
username on create; `'Unknown'` when the annotation has no user).

### Status codes

| Situation | Code |
|---|---|
| Create document / annotation | `201` |
| Update / delete annotation | `204` |
| Document not found **or** no document access (get / export / create annotation) | `404` |
| No document access on `GET .../annotations` (list) | `403` |
| Update/delete: annotation not found | `404` |
| Update/delete: found but not permitted by policy | `403` |

### Create example

```json
POST /api/annotations
{
  "pdf_document_id": 7,
  "annotation": {
    "page_number": 0,
    "annotation_type": "text",
    "annotation_data": {
      "rect": [100, 200, 300, 240],
      "content": "Approved",
      "colors": { "stroke": [0, 0, 0] },
      "fontSize": 16
    }
  }
}
```

### Multipart upload example

```bash
curl -X POST "https://host/api/pdf-documents?pdf_title=Contract" \
     -F "pdf_file=@contract.pdf"
```

## Mounting the router

```python
from ninja import NinjaAPI
from sherbert_pdf.api import router as sherbert_pdf_router

api = NinjaAPI()                       # attach host auth here (see below)
api.add_router("", sherbert_pdf_router)

urlpatterns = [
    path("api/", api.urls),
]
```

**Attaching host authentication.** The router endpoints read `request.user`
(they call `check_pdf_access(request.user, ...)`), so the host must ensure an
authenticated user. Attach Ninja auth on the `NinjaAPI` (or per-router), e.g.:

```python
from ninja.security import django_auth
api = NinjaAPI(auth=django_auth)       # require a logged-in Django session
api.add_router("", sherbert_pdf_router)
```

**`SHERBERT_PDF_API_BASE`** (default `"/api"`) — the URL prefix the **editor**
uses to reach the router. If you mount the router somewhere other than `/api`,
set this so the editor's fetch calls and Export link resolve:

```python
SHERBERT_PDF_API_BASE = "/documents/api"
```

## Access control

Access decisions go through a pluggable policy (`sherbert_pdf/access.py`). The
module-level helpers do the lookup + `DoesNotExist` handling, then delegate the
decision to the active policy.

**Default policy — `AccessPolicy` (owner-only):**

- `can_access_document(user, pdf_document) -> bool` — `True` iff
  `pdf_document.user == user`.
- `can_modify_annotation(user, annotation) -> (bool, error_message_or_None)` —
  requires document access **and** `annotation.user_id == user.id`; returns
  `(False, 'Access denied to PDF document')` or
  `(False, 'You can only modify your own annotations')` otherwise.

**`SHERBERT_PDF_ACCESS_POLICY`** (default `None`) — a dotted path to your own
`AccessPolicy` subclass (or any class with the same two methods). It is
instantiated fresh on every call (no caching, so `override_settings` works in
tests).

```python
# settings.py
SHERBERT_PDF_ACCESS_POLICY = "myapp.access.ReviewerAccessPolicy"
```

```python
# myapp/access.py
from sherbert_pdf.access import AccessPolicy

class ReviewerAccessPolicy(AccessPolicy):
    """Owner OR anyone in the 'reviewers' group may view/annotate."""

    def can_access_document(self, user, pdf_document) -> bool:
        if super().can_access_document(user, pdf_document):
            return True
        return user.is_authenticated and user.groups.filter(name="reviewers").exists()

    # can_modify_annotation is inherited: it calls can_access_document above,
    # then still restricts edits to each annotation's own author.
```

Helper functions (stable signatures, used by the API and `EditorView`):

- `check_pdf_access(user, pdf_document_id=None, pdf_document=None) -> (has_access, pdf_document_or_None)`
- `check_annotation_ownership(user, annotation_id=None, annotation=None) -> (is_owner, annotation_or_None, error_message_or_None)`

## The editor

`EditorView` (a `TemplateView`) renders the standalone editor at
`sherbert_pdf/editor.html`. Include the app's URLs:

```python
urlpatterns = [
    path("pdf/", include("sherbert_pdf.urls")),
]
```

- URL name: **`sherbert_pdf:editor`** (the urlconf sets `app_name = 'sherbert_pdf'`).
- Path: `editor/<int:pk>/` (under your include prefix, e.g. `/pdf/editor/7/`).
- Access: `EditorView` calls `check_pdf_access`; denial raises `Http404` (so the
  existence of documents is not leaked).

```html
<a href="{% url 'sherbert_pdf:editor' doc.pk %}">Edit</a>
```

**Template / static requirements.** The editor template loads, from CDNs:
`pdf.js` 3.11.174 (renders PDF pages) and `Konva` 9.3.16 (annotation canvas). It
also loads the packaged static assets `sherbert_pdf/editor.css` and
`sherbert_pdf/editor.js` (an ES module) — so `django.contrib.staticfiles` must
be installed and static serving working. The page passes an `editor_config` JSON
blob (`pdfId`, `fileUrl`, `apiBase`, `userId`, `stamps`) to the JS.

## Stamps

**`SHERBERT_PDF_STAMPS`** (default: two packaged demo stamps) — the stamp palette
shown in the editor. A list of `{"label", "url"}` dicts:

```python
from django.templatetags.static import static

SHERBERT_PDF_STAMPS = [
    {"label": "Approved", "url": static("sherbert_pdf/stamps/approved.png")},
    {"label": "Rejected", "url": static("sherbert_pdf/stamps/rejected.png")},
    {"label": "Confidential", "url": static("myapp/stamps/confidential.png")},
]
```

If unset (or empty), the editor falls back to the two built-in demo stamps
(`approved.png`, `rejected.png`) shipped in the package's static dir, resolved
through `static()` so they honor the host's `STATIC_URL`.

## Export

`PDFDocument.export()` (and `GET /pdf-documents/{id}/export`) returns
`bytes`: the original PDF with every annotation flattened onto the pages via
PyMuPDF, then `tobytes(garbage=4, deflate=True)`. Rendering per type:

| Type | How it renders (PyMuPDF) |
|---|---|
| `pen` | `add_ink_annot` from `vertices[0]`; stroke color, border width, opacity applied. |
| `highlighter` | Same as pen, but **opacity forced to `0.3`**. |
| `text` | `insert_htmlbox` into `rect` with an HTML snippet (color, size, weight, line-height); cursive/script font → italic. |
| `stamp` | `insert_image` into `[x, y, x+width, y+height]` from the resolved image file. |
| `cloud` | New format `[x,y,w,h]` → scalloped outline via `add_ink_annot`; legacy point-list drawn directly. |

Rendering is defensive: any single annotation that errors is logged and skipped,
so one bad annotation does not fail the whole export.

**Stamp image resolution.** `imageUrl` is turned into a filesystem path by
trying, in order: strip scheme/host and the `STATIC_URL` prefix, then look up via
(1) the **staticfiles finders** (so packaged app-static images like the demo
stamps resolve in development without `collectstatic`), (2) `STATIC_ROOT`, (3)
`BASE_DIR/static/`. If none exist, the stamp is skipped and a warning is logged.

> **Export caveat (verified in source):** the export code for pen/highlighter/
> cloud reads `vertices`, `colors`, `border`, and `opacity` but **does not apply
> `erasures`**. Eraser holes are stored on the annotation and rendered live in
> the editor, but they are **not** subtracted from the exported PDF.

## Editor capabilities and limitations

Tools in the toolbar (`editor.html` / `editor.js`): **pen**, **highlighter**,
**text**, **signature** (a cursive-styled text tool, stored as a `text`
annotation), **stamp**, **revision cloud**, **eraser**, and **select/move**.
Plus per-tool color swatches and a size slider, undo/redo, zoom, and Export.

- **Zoom is per-axis / anchor-aware.** Pinch and toolbar ± zoom re-anchor scroll
  position per-axis (X and Y independently) around the cursor/anchor page;
  toolbar/programmatic zoom commits immediately (`ZOOM_STEP = 1.25`, base
  `RENDER_SCALE = 1.5`).
- **Touch / stylus supported.** Input is handled via pointer events (mouse,
  touch, and stylus) with pinch-zoom.
- **Undo/redo** replays create/delete/update commands against the API
  (Ctrl+Z / Ctrl+Shift+Z).
- **Programmatic hook:** the editor exposes `window.__sherbertEditor` (with
  `ready`, `zoom()`, `setZoom(z)`, `nodeCount(pageIndex)`, etc.) — used by the
  e2e tests.

Known limitations (verified in source):

- **The eraser is NOT in the undo stack.** Erasing modifies strokes and PUTs
  them to the API but never calls the undo-command push, so Undo will not reverse
  an erase.
- **Eraser only affects the current user's own pen/highlighter strokes**
  (`meta.isOwner` and stroke type are checked); it cannot erase text, stamps,
  clouds, or other users' strokes.
- **Erased-highlighter blend:** highlighters are drawn with
  `globalCompositeOperation: 'multiply'` at `0.3` opacity in the editor, and the
  eraser punches `destination-out` holes inside a cached group. Erasures are a
  live-editor effect only and, as noted above, **do not appear in the exported
  PDF**.
</content>
