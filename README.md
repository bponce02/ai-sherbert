# orange-sherbert

Two reusable Django apps shipped in one wheel:

- **`orange_sherbert`** — declarative CRUD views. Subclass `CRUDView`, set a few class attributes, call `get_urls()`, and you get list/detail/create/update/delete pages with search, filtering, column sorting, HTMX-powered inline formsets (including a "sequential" one-at-a-time mode), multi-column form layouts, per-field widget styling, and per-field/per-view permission gating. Templates are Tailwind + DaisyUI.
- **`sherbert_pdf`** — a PDF annotation backend plus a self-contained Konva.js editor. Store pen / highlighter / text / stamp / revision-cloud annotations against uploaded PDFs, expose them through a mountable Django-Ninja router, and export a flattened annotated PDF via PyMuPDF. Optional; installed with the `pdf` extra.

The two apps are independent — you can install and use either alone.

Full reference documentation:

- [`docs/orange-sherbert.md`](docs/orange-sherbert.md) — complete `CRUDView` reference.
- [`docs/sherbert-pdf.md`](docs/sherbert-pdf.md) — complete `sherbert_pdf` reference (models, REST API, editor, export, access control).

## Requirements

| | |
|---|---|
| Python | `>=3.12` |
| Django | `>=5.2` |
| `orange_sherbert` runtime deps | `django-htmx>=1.27.0` (installed automatically) |
| `sherbert_pdf` runtime deps | `pymupdf>=1.26`, `django-ninja>=1.5` (only via the `pdf` extra) |

## Install

Install from git (the package is not on PyPI):

```bash
# pip
pip install "orange-sherbert @ git+https://github.com/bponce02/ai-sherbert.git"

# pip, with the PDF app's extra deps (pymupdf + django-ninja)
pip install "orange-sherbert[pdf] @ git+https://github.com/bponce02/ai-sherbert.git"

# uv
uv add "orange-sherbert @ git+https://github.com/bponce02/ai-sherbert.git"
uv add "orange-sherbert[pdf] @ git+https://github.com/bponce02/ai-sherbert.git"
```

The distribution installs **two** top-level importable packages: `orange_sherbert` and `sherbert_pdf`.

## Configure the host project

### `orange_sherbert`

`INSTALLED_APPS` and the django-htmx middleware are both required. The inline-formset add/save/delete flows POST via HTMX and rely on `request.htmx` set by `django_htmx.middleware.HtmxMiddleware`:

```python
INSTALLED_APPS = [
    # ...
    "django.contrib.staticfiles",
    "django_htmx",
    "orange_sherbert",
    # your apps
]

MIDDLEWARE = [
    # ...
    "django_htmx.middleware.HtmxMiddleware",   # REQUIRED for orange_sherbert
]
```

`orange_sherbert` has **no models and no migrations** — it is views + templates + template tags only.

**Templates require Tailwind CSS and DaisyUI at render time.** The bundled base template (`orange_sherbert/base.html`) pulls both from a CDN, so the default pages work out of the box; if you override templates, supply Tailwind + DaisyUI yourself. See [`docs/orange-sherbert.md` → Template overrides](docs/orange-sherbert.md#template-overrides).

### `sherbert_pdf`

```python
INSTALLED_APPS = [
    # ...
    "django.contrib.staticfiles",   # needed to resolve packaged stamp images
    "sherbert_pdf",
]
```

Then run migrations (this app **does** define models):

```bash
python manage.py migrate sherbert_pdf
```

## Quickstart: `orange_sherbert`

Define a `CRUDView` subclass and wire its URLs. This is drawn from `src/example/`.

```python
# views.py
from orange_sherbert.view import CRUDView
from .models import Book

class BookCRUDView(CRUDView):
    model = Book
    fields = {                      # {field_name: column header} shown in list/detail
        "title": "Title",
        "author": "Author",
        "pub_date": "Publication Date",
    }
    search_fields = ["title", "isbn"]
    filter_fields = {"author": "Author"}
```

```python
# urls.py
from .views import BookCRUDView

urlpatterns = [
    *BookCRUDView.get_urls(),       # expands to 5 named URL patterns
]
```

`get_urls()` produces, for `model_name = "book"`:

| URL name | Path | View |
|---|---|---|
| `book-list` | `book/` | list |
| `book-create` | `book/create/` | create |
| `book-detail` | `book/<int:pk>/` | detail |
| `book-update` | `book/<int:pk>/update/` | update |
| `book-delete` | `book/<int:pk>/delete/` | delete |

See [`docs/orange-sherbert.md`](docs/orange-sherbert.md) for every attribute and hook.

## Quickstart: `sherbert_pdf`

`sherbert_pdf` ships a REST API as a **mountable Django-Ninja `Router`** (it deliberately attaches no authentication of its own — the host mounts it on a `NinjaAPI` and supplies auth), plus a standalone editor page.

```python
# urls.py
from django.urls import include, path
from ninja import NinjaAPI
from sherbert_pdf.api import router as sherbert_pdf_router

api = NinjaAPI()                       # attach your host's auth here
api.add_router("", sherbert_pdf_router)

urlpatterns = [
    path("api/", api.urls),            # REST endpoints under /api/...
    path("pdf/", include("sherbert_pdf.urls")),   # editor at /pdf/editor/<pk>/
]
```

The editor page is served by `sherbert_pdf.urls` at URL name `sherbert_pdf:editor` (`pdf/editor/<int:pk>/`). It loads Konva.js and pdf.js from a CDN and talks to the router. By default it calls the API at `/api`; override with the `SHERBERT_PDF_API_BASE` setting if you mount the router elsewhere.

See [`docs/sherbert-pdf.md`](docs/sherbert-pdf.md) for the full endpoint list, annotation payload shapes, access-control policy, and export details.

## Settings reference

| Setting | App | Default | Purpose |
|---|---|---|---|
| `ORANGE_SHERBERT_FIELD_WIDGETS` | orange_sherbert | `DEFAULT_FIELD_WIDGETS` (see [docs](docs/orange-sherbert.md#field-widgets-and-orange_sherbert_field_widgets)) | Map Django form-field types → `(widget_class_name, css_classes, extra_attrs)`. |
| `SHERBERT_PDF_ACCESS_POLICY` | sherbert_pdf | `None` → built-in owner-only `AccessPolicy` | Dotted path to an `AccessPolicy` subclass. |
| `SHERBERT_PDF_API_BASE` | sherbert_pdf | `"/api"` | URL prefix the editor uses to reach the mounted router. |
| `SHERBERT_PDF_STAMPS` | sherbert_pdf | two packaged demo stamps | List of `{"label", "url"}` stamp definitions for the editor palette. |

## Run the example project

`src/example/` is a working demo (a small "Books" library plus PDF documents). From the repo root:

```bash
uv sync --extra pdf                       # or: pip install -e ".[pdf]" + dev deps
cd src
uv run python manage.py migrate
uv run python manage.py runserver
```

Then visit:

- `/book/` — the `orange_sherbert` CRUD demo.
- `/pdfs/` — upload a PDF, then open the `sherbert_pdf` editor.

## Run the tests

```bash
uv run pytest
```

Test settings are `example.settings` (see `pytest.ini`); tests live in `src/test/`.
</content>
