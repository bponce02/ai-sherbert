from django import template
from django.core.exceptions import FieldDoesNotExist

register = template.Library()


@register.filter
def get_item(dictionary, key):
    """Get a value from a dict using a variable key. Usage: {{ my_dict|get_item:key }}"""
    if not dictionary:
        return None
    return dictionary.get(key)


@register.filter
def get_attr(obj, attr):
    """Get an attribute from an object using a variable name. Usage: {{ obj|get_attr:attr_name }}"""
    return getattr(obj, attr, '')


@register.simple_tag
def has_perm(user, permission):
    """Return True if ``user`` holds ``permission`` (an ``app_label.codename``
    string). Usage: ``{% has_perm user action.permission as allowed %}``.
    An empty/None permission is treated as "no restriction" → True."""
    if not permission:
        return True
    return user.has_perm(permission)


@register.simple_tag
def get_field_options(obj, field_name):
    model = obj.model
    
    if '__' in field_name:
        parts = field_name.split('__')
        current_model = model
        for part in parts[:-1]:
            rel_field = current_model._meta.get_field(part)
            current_model = rel_field.related_model
        field = current_model._meta.get_field(parts[-1])
        
        distinct_values = model.objects.values_list(field_name, flat=True).distinct()
        distinct_values = [v for v in distinct_values if v not in (None, '')]
        
        if hasattr(field, 'choices') and field.choices:
            choices_dict = dict(field.choices)
            return [(v, choices_dict.get(v, v)) for v in distinct_values]
        return [(v, v) for v in distinct_values]
    
    field = model._meta.get_field(field_name)
    
    if field.is_relation:
        related_model = field.related_model
        related_ids = model.objects.values_list(field_name, flat=True).distinct()
        related_ids = [v for v in related_ids if v is not None]
        related_objects = related_model.objects.filter(pk__in=related_ids)
        return [(obj.pk, str(obj)) for obj in related_objects]
    
    distinct_values = model.objects.values_list(field_name, flat=True).distinct().order_by(field_name)
    return [(v, v) for v in distinct_values if v not in (None, '')]


@register.simple_tag
def is_selected(option, request, field):
    current = request.GET.get(field, '')
    return 'selected' if str(option) == str(current) else ''


@register.simple_tag
def get_verbose_name(obj, field_name):
    """
    Get the verbose name for a field from a model instance.
    
    Usage: {% get_verbose_name object 'author' %}
    Returns: The field's verbose_name exactly as defined in the model
    """
    model = obj._meta.model
    try:
        field = model._meta.get_field(field_name)
        return field.verbose_name
    except FieldDoesNotExist:
        return field_name.replace('_', ' ').title()




# ---------------------------------------------------------------------------
# <c-sherbert.field> support: widget dispatch + theme class lookup
# ---------------------------------------------------------------------------

# widget_type -> control component name (templates/cotton/sherbert/controls/<name>.html)
SHERBERT_BUILTIN_KINDS = {
    'text': 'input',
    'email': 'input',
    'url': 'input',
    'number': 'input',
    'password': 'input',
    'date': 'input',
    'time': 'input',
    'datetime': 'input',
    'select': 'select',
    'selectmultiple': 'select',
    'textarea': 'textarea',
    'file': 'file',
    'clearablefile': 'file',
    'checkbox': 'checkbox',
    'radioselect': 'radio_list',
    'checkboxselectmultiple': 'checkbox_list',
}

# widget_type -> HTML5 input type to force (Django renders these as type="text")
SHERBERT_HTML5_TYPES = {
    'date': 'date',
    'time': 'time',
    'datetime': 'datetime-local',
}


def _control_template_exists(name):
    from django.template import TemplateDoesNotExist
    from django.template.loader import get_template

    try:
        get_template(f'cotton/sherbert/controls/{name}.html')
    except TemplateDoesNotExist:
        return False
    return True


@register.simple_tag
def sherbert_kind(field, kinds=None):
    """Resolve which ``cotton/sherbert/controls/<kind>.html`` renders ``field``.

    Lookup order:
      1. ``kinds`` alias dict passed by the caller (``{'tomselect': 'select'}``)
      2. a control template named after the widget type (project-provided
         ``controls/proseeditor.html`` etc.)
      3. built-in widget types
      4. any widget that renders an ``<input>`` (has ``input_type``) -> ``input``
      5. ``raw`` — render the widget untouched (third-party editors, pickers…)
    """
    wt = field.widget_type
    if kinds and wt in kinds:
        return kinds[wt]
    if _control_template_exists(wt):
        return wt
    if wt in SHERBERT_BUILTIN_KINDS:
        return SHERBERT_BUILTIN_KINDS[wt]
    if getattr(field.field.widget, 'input_type', None):
        return 'input'
    return 'raw'


@register.simple_tag
def sherbert_itype(field):
    """HTML5 ``type`` to force on date/time widgets, or '' to leave it alone."""
    return SHERBERT_HTML5_TYPES.get(field.widget_type, '')


@register.filter(name='cls')
def sherbert_class(classes, key):
    """``classes|cls:"input"`` -> the theme class string for ``key`` ('' if unset).

    Unlike ``get_item`` this never yields None, so it is safe to chain into
    widget-tweaks' ``add_class``.
    """
    if not classes or not key:
        return ''
    return classes.get(key) or ''


@register.simple_tag
def sherbert_variant(classes, kind, suffix):
    """``{% sherbert_variant classes kind size as size_class %}`` ->
    ``classes["<kind>_<suffix>"]`` ('' when suffix or key is missing). Used for
    size (``input_sm``) and error (``input_error``) variants."""
    if not classes or not suffix:
        return ''
    return classes.get(f'{kind}_{suffix}') or ''
