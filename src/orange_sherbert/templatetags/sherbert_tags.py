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


