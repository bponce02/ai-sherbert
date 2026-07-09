# `orange_sherbert` — `CRUDView` reference

`orange_sherbert.view.CRUDView` is a single declarative class. You subclass it,
set class attributes, and call `.get_urls()` to mount five pages
(list / detail / create / update / delete). One subclass handles all five view
types; the concrete Django generic view is chosen per-request by
`CRUDView.dispatch()` based on the `view_type` passed to `as_view()`.

```python
from orange_sherbert.view import CRUDView
```

- No models, no migrations, no settings are required for the base feature set.
- Requires `django_htmx` in `INSTALLED_APPS` and its middleware (see README).
- Templates use Tailwind + DaisyUI.

## Contents

- [Attribute quick reference](#attribute-quick-reference)
- [`model` and `fields`](#model-and-fields)
- [`form_fields`](#form_fields)
- [URL wiring: `get_urls`, `url_prefix`, `url_namespace`, `path_converter`](#url-wiring)
- [`filter_fields`](#filter_fields)
- [`search_fields`](#search_fields)
- [`property_field_map`](#property_field_map)
- [`restricted_fields`](#restricted_fields)
- [`enforce_model_permissions`](#enforce_model_permissions)
- [`extra_actions`](#extra_actions)
- [`field_widths` and `cell_css`](#field_widths-and-cell_css)
- [`form_layout`](#form_layout)
- [`inline_formsets`](#inline_formsets)
- [Template overrides](#template-overrides)
- [Side cards / top cards](#side-cards--top-cards)
- [Parent-view hooks](#parent-view-hooks)
- [`field_widgets` and `ORANGE_SHERBERT_FIELD_WIDGETS`](#field-widgets-and-orange_sherbert_field_widgets)
- [List page behaviors](#list-page-behaviors)
- [Template tags](#template-tags)

## Attribute quick reference

Every configurable class attribute on `CRUDView`, with its default:

| Attribute | Default | Summary |
|---|---|---|
| `model` | *(required)* | The Django model class. |
| `fields` | `[]` | List/detail display fields: `{name: verbose}` dict, or `"__all__"`. |
| `form_fields` | `{}` | Create/update form fields: `{name: verbose}` dict. Falls back to `fields` if empty. |
| `form_class` | *(unset)* | Optional custom `ModelForm`; overrides `form_fields` on create/update. |
| `enforce_model_permissions` | `False` | Gate each view behind Django model permissions. |
| `restricted_fields` | `{}` | `{field: permission}` — hide a field from users lacking the permission. |
| `filter_fields` | `{}` | `{field: label}` — exact-match dropdown filters on the list page. |
| `search_fields` | `[]` | List of fields for the `icontains` OR search box. |
| `property_field_map` | `{}` | `{display_name: db_field}` — sortable computed properties + form-field resolution. |
| `extra_actions` | `[]` | Per-object custom action buttons/URLs. |
| `field_widgets` | `{}` | Per-view `{field_name: (widget, css, attrs)}` widget overrides. |
| `field_widths` | `{}` | Per-column width. Dict `{field: n}` or positional list. |
| `cell_css` | `{}` | Per-column `<td>` CSS. Dict `{field: css}` or positional list. |
| `form_layout` | `None` | List of field-name lists → columns on the create/update form. |
| `inline_formsets` | `[]` | Related-object formsets on the create/update form. |
| `url_prefix` | `None` | Override URL path/name base (defaults to model name). |
| `url_namespace` | `None` | Namespace prefix for reversing URLs. |
| `path_converter` | `'int'` | Path converter for the `<pk>` segment. |
| `list_template_name` | `'orange_sherbert/list.html'` | List template. |
| `detail_template_name` | `'orange_sherbert/detail.html'` | Detail template. |
| `create_template_name` | `'orange_sherbert/create.html'` | Create template. |
| `update_template_name` | `'orange_sherbert/update.html'` | Update template. |
| `delete_template_name` | `'orange_sherbert/delete.html'` | Delete template. |
| `side_cards_template` | `None` | Template rendered beside create/update/detail forms. |
| `top_cards_template` | `None` | Template rendered above create/update/detail forms. |
| `side_cards_on_create` | `True` | Show side cards on the create view. |
| `top_cards_on_create` | `True` | Show top cards on the create view. |
| `sequential_on_create` | `False` | Show sequential formsets on the create view. |

Hooks a subclass may additionally *define* (they are not attributes with
defaults — they are called if present): `get_queryset`, `get_form`,
`get_form_kwargs`, `get_context_data`, `form_valid`, `post_save`,
`get_post_create_url`, `get_sequential_context`. See
[Parent-view hooks](#parent-view-hooks).

## `model` and `fields`

`fields` controls which fields appear on the **list** and **detail** pages (as
columns / rows). Two accepted forms:

**Dict `{field_name: column_header}`** — order and labels are yours:

```python
class BookCRUDView(CRUDView):
    model = Book
    fields = {
        "title": "Title",
        "author": "Author",
        "isbn": "ISBN",
        "pub_date": "Publication Date",
        "checked_out": "Checked Out",
    }
```

**`"__all__"`** — expands to every concrete model field except the primary key,
using each field's `verbose_name` as the header:

```python
class AuthorCRUDView(CRUDView):
    model = Author
    fields = "__all__"
    search_fields = ["name"]
```

Field names may be **model properties** (e.g. `formatted_price`) or related
lookups — the value shown is `getattr(obj, field_name, '')`. Properties are not
sortable or form-editable unless mapped via
[`property_field_map`](#property_field_map).

## `form_fields`

`form_fields` is a `{field_name: label}` dict controlling which fields appear on
the **create/update forms** — independent of the display `fields`. If
`form_fields` is empty (`{}`), the form falls back to using `fields`.

Use this when the form should edit fields not shown in the list, or edit the
real DB column behind a display property:

```python
class BookCRUDView(CRUDView):
    model = Book
    fields = {"title": "Title", "formatted_price": "Price"}   # list shows a property
    form_fields = {                                            # form edits real columns
        "title": "Title",
        "price": "Price",
        "pub_date": "Publication Date",
        "checked_out": "Checked Out",
        "location": "Location",
    }
```

The label values (`"Title"`, …) are carried into the generated form's field
config. To supply a fully custom form instead, set `form_class` to a
`ModelForm` subclass — when present and the view is create/update, `form_class`
is used and `form_fields`/`fields` are ignored for the form.

## URL wiring

`CRUDView.get_urls()` is a **classmethod** returning a list of `path()` objects.
Splat it into `urlpatterns`:

```python
urlpatterns = [
    *BookCRUDView.get_urls(),
]
```

Generated patterns (base = `url_prefix` if set, else `model._meta.model_name`):

| URL name | Path |
|---|---|
| `{base}-list` | `{base}/` |
| `{base}-create` | `{base}/create/` |
| `{base}-detail` | `{base}/<{path_converter}:pk>/` |
| `{base}-update` | `{base}/<{path_converter}:pk>/update/` |
| `{base}-delete` | `{base}/<{path_converter}:pk>/delete/` |
| `{base}-{action_name}` | `{base}/<{path_converter}:pk>/{action_name}/` | *(one per `extra_actions` entry)* |

**`url_prefix`** — override both the path segment and the URL-name base:

```python
class BookCRUDView(CRUDView):
    model = Book
    url_prefix = "library-books"     # → library-books/, name "library-books-list", ...
```

**`path_converter`** — the converter used for the `<pk>` segment. Default
`'int'`. Set to `'uuid'`, `'slug'`, `'str'`, etc. for non-integer PKs:

```python
class OrderCRUDView(CRUDView):
    model = Order
    path_converter = "uuid"          # → order/<uuid:pk>/
```

**`url_namespace`** — a namespace string used when the view reverses its own
URLs (e.g. the success-URL redirect after save, and links in templates). Set it
to the `app_name`/`namespace` you include the URLs under so reversing resolves.
The success URL after create/update is `{url_namespace}:{model_name}-list` when
`url_namespace` is set, else `{model_name}-list`.

## `filter_fields`

`{field_name: label}` — renders a `<select>` per field on the list page whose
options are the distinct existing values; selecting one applies an **exact**
`queryset.filter(field=value)`. The current value comes from
`request.GET[field_name]`.

```python
filter_fields = {"author": "Author", "checked_out": "Checked Out"}
```

Options are auto-populated by the `get_field_options` template tag (handles
choices, relations via `__str__`, and `__`-spanning lookups).

## `search_fields`

A list of field names. The list page shows a search box; a non-empty `search`
query param builds an OR of `field__icontains=query` across all listed fields:

```python
search_fields = ["title", "isbn"]
```

Only these two query mechanisms filter the list: `filter_fields` (exact) and
`search_fields` (icontains OR). They compose (AND together).

## `property_field_map`

`{display_name: db_field_name}` — bridges a computed **property** (shown in
`fields`) to a real **DB column**, for two purposes:

1. **Sorting.** When the list is sorted by `sort_by=display_name`, ordering is
   applied on the mapped DB field: `order_by(db_field)`. Without the mapping,
   sorting a property raises a DB error (properties are not columns).
2. **Form-field resolution.** On create/update, if a `form_fields` key is a
   property in the map, it is replaced by the mapped DB field so the form edits
   the real column. `form_layout` names are resolved through the map too.

```python
class Book(models.Model):
    price = models.DecimalField(max_digits=10, decimal_places=2)
    @property
    def formatted_price(self):
        return f"${self.price:,}"

class BookCRUDView(CRUDView):
    model = Book
    fields = {"formatted_price": "Price"}       # display the property
    property_field_map = {"formatted_price": "price"}   # sort/edit via real column
```

## `restricted_fields`

`{field_name: permission_codename}` — if the requesting user lacks the
permission, the field is **removed** from both the display `fields` and the
`form_fields` for that request (the field simply disappears; no error). Checked
via `request.user.has_perm(permission)`.

```python
class BookCRUDView(CRUDView):
    model = Book
    fields = {"title": "Title", "ordered_from": "Ordered From"}
    restricted_fields = {"ordered_from": "example.can_view_ordered_from"}
```

The permission string is passed straight to `has_perm`; use a fully-qualified
`app_label.codename` unless you rely on Django's default app resolution.
Fields removed by `restricted_fields` are silently skipped by `form_layout`
(they are not treated as typos).

## `enforce_model_permissions`

`False` by default. When `True`, each view type is gated behind the standard
Django model permission for its action, and users without it get an
`HttpResponseForbidden` ("You do not have permission to perform this action."):

| view_type | required permission |
|---|---|
| list, detail | `{app_label}.view_{model}` |
| create | `{app_label}.add_{model}` |
| update | `{app_label}.change_{model}` |
| delete | `{app_label}.delete_{model}` |

```python
class BookCRUDView(CRUDView):
    model = Book
    enforce_model_permissions = True
```

## `extra_actions`

A list of dicts adding per-object action buttons on the list page and a URL for
each. Each entry:

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | URL slug + name suffix; URL name is `{base}-{name}`. |
| `view` | yes | A view **class** (`.as_view()` is called). |
| `label` | yes (for the button) | Button text on the list page. |
| `method` | yes (for the button) | `"GET"` → renders an `<a>`; anything else → a POST `<form>` with CSRF. |
| `permission` | optional | `app_label.codename` string; enforced on both the URL and the button (see note). |

```python
class OrderOnlineView(View):
    def post(self, request, pk):
        book = Book.objects.get(pk=pk)
        return redirect(f"https://www.barnesandnoble.com/s/{book.title}")

class BookCRUDView(CRUDView):
    model = Book
    extra_actions = [
        {"name": "order-online", "view": OrderOnlineView, "label": "Order Online", "method": "POST"},
    ]
```

This generates the URL `book/<int:pk>/order-online/` named `book-order-online`.
The action view receives `pk` as a URL kwarg.

### Action permissions

An action may carry a `permission` key (an `app_label.codename` string). When
set, it is enforced in two places:

- **URL** — `get_urls()` wraps the action view so a request from a user lacking
  the permission (`request.user.has_perm(permission)`) gets an
  `HttpResponseForbidden` before the action view runs. The wrapper is
  transparent otherwise: the action view's own kwargs and CSRF handling are
  unchanged.
- **List button** — the action's button/form is only rendered for users who
  hold the permission (via the `has_perm` template tag in `sherbert_tags`).

Actions **without** a `permission` key remain visible and callable by everyone
(backward compatible). Because the check uses `has_perm`, a superuser passes all
permission gates as usual.

```python
class BookCRUDView(CRUDView):
    model = Book
    extra_actions = [
        {"name": "check-out", "view": CheckOutView, "label": "Check Out",
         "method": "POST", "permission": "example.can_check_out"},
    ]
```

The permission is checked at the action layer only; the standard list/detail/
create/update/delete views are gated separately by
[`enforce_model_permissions`](#enforce_model_permissions).

## `field_widths` and `cell_css`

Both accept **either** a `{field_name: value}` dict **or** a positional
list/tuple. A positional list is zipped against the list view's display fields
in order (`_map_to_fields`): index `i` maps to the i-th key of `fields`.

- `field_widths` — a per-column width value applied on the list table columns.
- `cell_css` — extra CSS classes applied to each column's `<td>`.

```python
class BookCRUDView(CRUDView):
    model = Book
    fields = {"title": "Title", "author": "Author", "isbn": "ISBN",
              "formatted_price": "Price", "pub_date": "Date", "checked_out": "Out"}
    # positional: title→20, author→10, isbn→10, price→5, date→5, checked_out→5
    field_widths = [20, 10, 10, 5, 5, 5]
    # dict: only these columns get extra <td> classes
    cell_css = {"formatted_price": "text-right font-bold", "checked_out": "text-center"}
```

## `form_layout`

`form_layout` arranges the create/update form into **columns**. It is a list of
lists; **each inner list is one column**, and the names in it are stacked
vertically in that column. This is the newest layout feature — prefer it over
manual template overrides for multi-column forms.

```python
class BookCRUDView(CRUDView):
    model = Book
    form_fields = {
        "title": "Title", "author": "Author", "isbn": "ISBN",
        "price": "Price", "pub_date": "Publication Date", "location": "Location",
        "checked_out": "Checked Out", "ordered_from": "Ordered From",
    }
    form_layout = [
        ["title", "author", "isbn"],        # column 1
        ["price", "pub_date", "location"],  # column 2
        ["checked_out", "ordered_from"],    # column 3
    ]
```

Behavior (verified in `_build_form_columns`):

- The number of columns equals `len(form_layout)`. Columns render in a CSS grid
  (`.sherbert-form-grid`, `--sherbert-form-cols: N`) that **stacks to a single
  column below the `md` breakpoint (768px)** — responsive by default.
- **Names are resolved through `property_field_map`** before lookup, so you may
  list a display-property name and it maps to the DB field.
- **Fields present on the model/form but omitted from `form_layout`** are
  rendered **full-width, below the columns** (the "leftover" fields), in form
  order. Hidden fields are excluded from leftovers.
- **A name that was removed for this user by `restricted_fields`** (or otherwise
  not in the bound form but valid on the model / declared in `form_fields`) is
  **silently skipped** — no error, no empty cell.
- **A name that is neither on the form, nor a model field, nor in
  `form_fields`** raises `django.core.exceptions.ImproperlyConfigured` — typos
  fail loudly rather than dropping a field.

`form_layout` only affects create/update (`view_type in ('create', 'update')`)
and only when a `form` is present.

## `inline_formsets`

`inline_formsets` is a list of config dicts attaching related-object formsets to
the create/update form. Two modes: the default nestable formset, and
`"sequential"`.

Config keys (verified in `get_formsets` / `_get_sequential_config`):

| Key | Default | Meaning |
|---|---|---|
| `model` | *(required)* | The related model class. |
| `fields` | `'__all__'` | Fields of the related model to edit. `'__all__'` excludes PK and the parent FK. |
| `extra` | `1` | Number of blank extra forms. |
| `can_delete` | `True` | Allow deleting existing rows. |
| `prefix` | `model._meta.model_name` | Formset prefix / lookup key. |
| `nested_under` | `None` | Prefix/model to nest this formset under (child-of-child). |
| `queryset_filter` | `None` | `**kwargs` dict filtering which related rows are shown/edited. |
| `mode` | `None` | `'sequential'` for one-at-a-time mode (see below). |
| `sequential_template` | `'orange_sherbert/includes/formset_sequential.html'` | Custom template for sequential mode. |

**Default (nested) mode** — rendered inside the main form; saved atomically when
the main form is submitted. Supports nesting via `nested_under` (a child formset
keyed to each parent form instance). "Add another" is an HTMX POST that returns
a fresh empty form.

```python
inline_formsets = [
    {"model": BookRequest, "fields": ["requester_name", "requester_email"], "extra": 1},
]
```

**Sequential mode** (`"mode": "sequential"`) — a one-at-a-time UI: a table of
already-saved items at the top, and a **single** add form below. It differs from
the default mode in important ways (verified in `view.py`):

- It is rendered **outside** the main form (its own `<div id="seq-{prefix}">`),
  and is **not bound to the main form's POST**. Saving the main form does not
  save sequential items.
- Each add/delete is its **own HTMX request** to the same URL, distinguished by
  hidden POST fields `formset_sequential_save` / `formset_sequential_delete`
  (plus `formset_name`, `item_pk`). The whole `#seq-{prefix}` block is swapped
  (`hx-swap="outerHTML"`) so list + add-form refresh together.
- Because saves need an existing parent, the add form only appears **after the
  parent object exists**. On the create view, sequential formsets are hidden
  unless `sequential_on_create = True`.

```python
class BookCRUDView(CRUDView):
    model = Book
    inline_formsets = [
        {
            "model": BookRequest,
            "fields": ["requester_name", "requester_email"],
            "can_delete": True,
            "mode": "sequential",
        },
    ]
    sequential_on_create = False   # sequential items are added after the book is saved
```

On the **detail** view, top-level (non-`nested_under`) inline formsets are
rendered read-only as `related_items` (respecting `queryset_filter`).

## Template overrides

Every page template is overridable by setting the corresponding attribute to
your own template path:

| Attribute | Default |
|---|---|
| `list_template_name` | `'orange_sherbert/list.html'` |
| `detail_template_name` | `'orange_sherbert/detail.html'` |
| `create_template_name` | `'orange_sherbert/create.html'` |
| `update_template_name` | `'orange_sherbert/update.html'` |
| `delete_template_name` | `'orange_sherbert/delete.html'` |

```python
class BookCRUDView(CRUDView):
    model = Book
    list_template_name = "myapp/book_list.html"
```

The bundled templates extend `orange_sherbert/base.html`, which loads Tailwind
+ DaisyUI from a CDN and sets `hx-headers` with the CSRF token. Reusable
includes live in `orange_sherbert/templates/orange_sherbert/includes/`
(`form.html`, `form_field.html`, `formset.html`, `formset_sequential.html`).
If you override the base or page templates, you must provide Tailwind + DaisyUI
yourself.

## Side cards / top cards

Small extra panels rendered around the create/update/detail form. Set a template
path; it receives the same context (including `object`, which is `None` on
create):

| Attribute | Default | Meaning |
|---|---|---|
| `side_cards_template` | `None` | Template rendered beside the form (a 3-col grid appears when set). |
| `top_cards_template` | `None` | Template rendered above the form. |
| `side_cards_on_create` | `True` | If `False`, side cards are suppressed on the create view. |
| `top_cards_on_create` | `True` | If `False`, top cards are suppressed on the create view. |

```python
class BookCRUDView(CRUDView):
    model = Book
    side_cards_template = "example/book_side_card.html"
    side_cards_on_create = False     # nothing to show before the book exists
    top_cards_on_create = False
```

## Parent-view hooks

Your `CRUDView` subclass is the **parent view**. The internal per-request Django
view calls these methods on your subclass **only if you define them**. Signatures
below are exactly how the mixin invokes them — match them precisely.

| Hook | Signature | Called when | Must return |
|---|---|---|---|
| `get_queryset` | `get_queryset(self, queryset, request)` | building the list/detail queryset (before filter/search/sort) | the (modified) queryset |
| `get_form` | `get_form(self, form, request)` | after the form is built and widget-styled | the (modified) form |
| `get_form_kwargs` | `get_form_kwargs(self)` | building form kwargs; `self.request` is set for you first | a dict merged into form kwargs |
| `get_context_data` | `get_context_data(self, context, request)` | after base context is built | the (modified) context dict |
| `form_valid` | `form_valid(self, form)` | on a valid submit, **before** save | *(return value ignored)* |
| `post_save` | `post_save(self, obj, request)` | inside the save transaction, after the object + formsets are saved | *(return value ignored)* |
| `get_post_create_url` | `get_post_create_url(self, obj)` | computing the redirect after a **create** | a URL string (falsy → default list URL) |
| `get_sequential_context` | `get_sequential_context(self, name, request)` | rendering a sequential formset block | a dict merged into that render's context (or falsy) |

```python
class BookCRUDView(CRUDView):
    model = Book

    def get_queryset(self, queryset, request):
        # e.g. scope to the current user
        return queryset.filter(owner=request.user)

    def get_form_kwargs(self):
        return {"request": self.request}          # merged into ModelForm kwargs

    def get_context_data(self, context, request):
        context["today"] = timezone.now().date()
        return context

    def post_save(self, obj, request):
        obj.log_saved_by(request.user)

    def get_post_create_url(self, obj):
        from django.urls import reverse
        return reverse("book-detail", args=[obj.pk])
```

Notes:
- `form_valid` (parent hook) runs **before** the object is saved; use
  `post_save` for logic that needs the saved instance and its formsets.
- The whole save (object + non-sequential formsets + `post_save`) runs inside a
  single `transaction.atomic()`.

## `field_widgets` and `ORANGE_SHERBERT_FIELD_WIDGETS`

Form widgets are styled automatically. Resolution order per form field
(`_apply_widget_styling_to_form`):

1. **Per-view `field_widgets`** — if the field name is a key here, its config
   wins.
2. **Global `ORANGE_SHERBERT_FIELD_WIDGETS`** (or `DEFAULT_FIELD_WIDGETS` if the
   setting is unset) — matched by the **form field's class name** (e.g.
   `CharField`, `DateField`, `ModelChoiceField`).

Each config is a **3-tuple**: `(widget_class_name, css_classes, extra_attrs)`.

- `widget_class_name` (str) is resolved in this order: an attribute of
  `orange_sherbert.widgets` (custom `DateInput`/`TimeInput`/`DateTimeInput` that
  set the HTML5 input type), then an attribute of `django.forms`, then a dotted
  import path.
- `css_classes` (str) becomes the widget's `class`. When a widget already has
  classes, the new classes are **merged** (union) rather than replacing.
- `extra_attrs` (dict) is applied as widget attributes (a `type` key is ignored).

**Per-view override:**

```python
class BookCRUDView(CRUDView):
    model = Book
    field_widgets = {
        "isbn": ("TextInput", "input input-bordered w-full font-mono", {"maxlength": "13"}),
    }
```

**Global override** in `settings.py` — keys are **form-field class names**:

```python
ORANGE_SHERBERT_FIELD_WIDGETS = {
    "DateField":   ("DateInput", "input input-bordered w-full", {}),
    "CharField":   ("TextInput", "input input-bordered w-full", {}),
    "DecimalField":("NumberInput", "input input-bordered w-full", {"step": "0.01"}),
    "TextField":   ("Textarea", "textarea textarea-bordered w-full", {"rows": "4"}),
    "BooleanField":("CheckboxInput", "checkbox", {}),
    # ... override only the ones you want; supply the whole dict.
}
```

`DEFAULT_FIELD_WIDGETS` (the fallback, from `orange_sherbert/defaults.py`) — the
exact built-in mapping:

| Form field class | Widget class | CSS classes | Extra attrs |
|---|---|---|---|
| `DateField` | `DateInput` | `input input-bordered w-full` | `{}` |
| `TimeField` | `TimeInput` | `input input-bordered w-full` | `{}` |
| `DateTimeField` | `DateTimeInput` | `input input-bordered w-full` | `{}` |
| `CharField` | `TextInput` | `input input-bordered w-full` | `{}` |
| `EmailField` | `EmailInput` | `input input-bordered w-full` | `{}` |
| `URLField` | `URLInput` | `input input-bordered w-full` | `{}` |
| `IntegerField` | `NumberInput` | `input input-bordered w-full` | `{}` |
| `DecimalField` | `NumberInput` | `input input-bordered w-full` | `{'step': '0.01'}` |
| `FloatField` | `NumberInput` | `input input-bordered w-full` | `{'step': 'any'}` |
| `TextField` | `Textarea` | `textarea textarea-bordered w-full` | `{'rows': '4'}` |
| `BooleanField` | `CheckboxInput` | `checkbox` | `{}` |
| `ChoiceField` | `Select` | `select select-bordered w-full` | `{}` |
| `TypedChoiceField` | `Select` | `select select-bordered w-full` | `{}` |
| `ModelChoiceField` | `Select` | `select select-bordered w-full` | `{}` |
| `ModelMultipleChoiceField` | `SelectMultiple` | `select select-bordered w-full` | `{'multiple': True}` |
| `FileField` | `FileInput` | `file-input file-input-bordered w-full` | `{}` |
| `ImageField` | `FileInput` | `file-input file-input-bordered w-full` | `{'accept': 'image/*'}` |

> Setting `ORANGE_SHERBERT_FIELD_WIDGETS` **replaces** the default dict entirely
> (there is no per-key merge with `DEFAULT_FIELD_WIDGETS`). Include every field
> type you want styled. The example above matches CoreCRM's real setting.

## List page behaviors

Query params the list view reads (`get_queryset` / `get_context_data`):

| Param | Effect |
|---|---|
| `search` | OR `icontains` across `search_fields`. |
| `<filter_field>` | Exact match for each `filter_fields` key present in the query. |
| `sort_by` | Column to sort by (resolved through `property_field_map`). |
| `sort_dir` | `'asc'` (default) or `'desc'`. |

**Session-preserved list filters.** When you navigate from the list to a
create/update/delete/detail page, the view stores the list's query string in the
session under `list_query_params_{model_name}` (read from the referer). The
"Cancel"/"Back" links and the post-save redirect append that saved query string,
so you return to the list with your search/filter/sort intact.

## Template tags

Load with `{% load sherbert_tags %}`. Available in
`orange_sherbert/templatetags/sherbert_tags.py`:

| Tag / filter | Usage | Returns |
|---|---|---|
| `get_item` (filter) | `{{ my_dict|get_item:key }}` | `dict.get(key)` with a variable key (`None` if dict is falsy). |
| `get_attr` (filter) | `{{ obj|get_attr:attr_name }}` | `getattr(obj, attr_name, '')` with a variable name. |
| `get_field_options` (tag) | `{% get_field_options queryset_or_view field_name %}` | List of `(value, label)` distinct options for filter dropdowns; handles choices, relations (`str(obj)`), and `__`-spanning lookups. Called on an object exposing `.model`. |
| `is_selected` (tag) | `{% is_selected option request field %}` | `'selected'` if `str(option) == request.GET[field]`, else `''`. |
| `get_verbose_name` (tag) | `{% get_verbose_name object 'author' %}` | The field's `verbose_name`; falls back to a title-cased field name if the field doesn't exist. |
| `has_perm` (tag) | `{% has_perm user action.permission as allowed %}` | `user.has_perm(permission)`; a falsy/empty permission returns `True` (no restriction). Used to gate `extra_actions` buttons. |
</content>
