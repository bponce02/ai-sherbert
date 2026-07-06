from django.views.generic import DetailView
from django.views.generic import ListView
from django.views.generic import CreateView
from django.views.generic import UpdateView
from django.views.generic import DeleteView
from django.views import View
from django.urls import path, reverse
from django.db import transaction
from django.db.models import Q, Model
from django.http import HttpResponseForbidden, HttpResponse
from django.template.loader import render_to_string
from django.forms.models import BaseInlineFormSet
from django.forms.models import inlineformset_factory

class NestedInlineFormSet(BaseInlineFormSet):
    parent_formset_name = None
    children = []
    queryset_filter = None
    def __init__(self, *args, parent_form=None, **kwargs):
        self.parent_form = parent_form
        # Apply queryset filter if defined and not already provided
        if self.queryset_filter and 'queryset' not in kwargs:
            kwargs['queryset'] = self.model.objects.filter(**self.queryset_filter)
        super().__init__(*args, **kwargs)

def nestedinlineformset_factory(parent_model, model, parent_formset_name, queryset_filter=None, **kwargs):
    FormSet = inlineformset_factory(
        parent_model,
        model,
        formset=NestedInlineFormSet,
        **kwargs
    )
    FormSet.parent_formset_name = parent_formset_name
    FormSet.queryset_filter = queryset_filter
    return FormSet

class _CRUDMixin:
    fields = None
    form_fields = None
    filter_fields = {}
    search_fields = []
    extra_actions = []
    property_field_map = {}
    view_type = None
    url_namespace = None
    inline_formsets = []
    parent_view = None
    field_widths = {}
    cell_css = {}
    form_layout = None

    def get_formsets(self):
        formsets = {}

        if self.inline_formsets:
            for config in self.inline_formsets:
                name = config.get('prefix', config['model']._meta.model_name)
                parent_model = config.get('nested_under') or self.model
                parent_name = config['nested_under']._meta.model_name if config.get('nested_under') else None

                formset = nestedinlineformset_factory(
                    parent_model,
                    config['model'],
                    parent_formset_name=parent_name,
                    queryset_filter=config.get('queryset_filter'),
                    fields=config.get('fields', '__all__'),
                    extra=config.get('extra', 1),
                    can_delete=config.get('can_delete', True),
                )
                formsets[name] = formset

        return formsets

    def _get_sequential_config(self, formset_name):
        return next(
            (c for c in (self.inline_formsets or [])
             if c.get('prefix', c['model']._meta.model_name) == formset_name
             and c.get('mode') == 'sequential'),
            None
        )

    def _get_sequential_display_fields(self, config):
        model = config['model']
        fields = config.get('fields', '__all__')

        if fields == '__all__':
            parent_fk_names = {
                f.name for f in model._meta.fields
                if f.is_relation and f.related_model == self.model
            }
            fields = [
                f.name for f in model._meta.fields
                if not f.primary_key and f.name not in parent_fk_names
            ]

        display_fields = []
        for field_name in fields:
            try:
                verbose = model._meta.get_field(field_name).verbose_name
            except Exception:
                verbose = field_name.replace('_', ' ')
            display_fields.append((str(verbose).capitalize(), field_name))

        return display_fields

    def _attach_sequential_meta(self, formset_instance, name, config):
        formset_instance.sequential_mode = True
        formset_instance.verbose_name_singular = config['model']._meta.verbose_name
        formset_instance.sequential_display_fields = self._get_sequential_display_fields(config)
        add_form = formset_instance.empty_form
        add_form.prefix = f'{name}-0'
        self._apply_widget_styling_to_form(add_form)
        formset_instance.sequential_add_form = add_form
        default_template = 'orange_sherbert/includes/formset_sequential.html'
        formset_instance.sequential_template = config.get('sequential_template', default_template)
        formset_instance.has_custom_layout = 'sequential_template' in config

    def _render_sequential_formset(self, request, config, formset_name, FormSetClass, error_formset=None):
        fresh_formset = FormSetClass(instance=self.object, prefix=formset_name)
        fresh_formset.model_name = formset_name
        fresh_formset.verbose_name = config['model']._meta.verbose_name_plural
        self._attach_sequential_meta(fresh_formset, formset_name, config)

        if error_formset is not None:
            bound_add_form = error_formset.forms[0] if error_formset.forms else fresh_formset.sequential_add_form
            self._apply_widget_styling_to_form(bound_add_form)
            fresh_formset.sequential_add_form = bound_add_form

        template = fresh_formset.sequential_template
        extra_context = {}
        if self.parent_view and hasattr(self.parent_view, 'get_sequential_context'):
            extra_context = self.parent_view.get_sequential_context(formset_name, request) or {}

        context = {'formset': fresh_formset, 'object': self.object}
        context.update(extra_context)
        html = render_to_string(template, context, request=request)
        return HttpResponse(html)

    def _handle_sequential_save(self, request):
        formset_name = request.POST.get('formset_name')
        self.object = self.get_object()

        config = self._get_sequential_config(formset_name)
        if not config:
            return HttpResponse(f"Sequential formset '{formset_name}' not found", status=400)

        FormSetClass = nestedinlineformset_factory(
            self.model,
            config['model'],
            parent_formset_name=None,
            queryset_filter=config.get('queryset_filter'),
            fields=config.get('fields', '__all__'),
            extra=1,
            can_delete=False,
        )

        bound = FormSetClass(request.POST, instance=self.object, prefix=formset_name)
        if bound.is_valid():
            bound.save()
            return self._render_sequential_formset(request, config, formset_name, FormSetClass)
        return self._render_sequential_formset(request, config, formset_name, FormSetClass, error_formset=bound)

    def _handle_sequential_delete(self, request):
        formset_name = request.POST.get('formset_name')
        item_pk = request.POST.get('item_pk')
        self.object = self.get_object()

        config = self._get_sequential_config(formset_name)
        if not config:
            return HttpResponse(f"Sequential formset '{formset_name}' not found", status=400)

        fk_field = None
        for field in config['model']._meta.fields:
            if field.related_model == self.model:
                fk_field = field.name
                break

        if not fk_field:
            return HttpResponse(
                f"Cannot determine relation between '{formset_name}' and parent model",
                status=400,
            )

        filter_kwargs = {'pk': item_pk, fk_field: self.object}
        filter_kwargs.update(config.get('queryset_filter', {}) or {})
        config['model'].objects.filter(**filter_kwargs).delete()

        FormSetClass = nestedinlineformset_factory(
            self.model,
            config['model'],
            parent_formset_name=None,
            queryset_filter=config.get('queryset_filter'),
            fields=config.get('fields', '__all__'),
            extra=1,
            can_delete=False,
        )
        return self._render_sequential_formset(request, config, formset_name, FormSetClass)

    def init_formsets(self):
        self.formset_instances = {}
        self.all_formsets_by_prefix = {}
        formsets = self.get_formsets()
        config_map = {c.get('prefix', c['model']._meta.model_name): c for c in (self.inline_formsets or [])}

        for name, FormSetClass in formsets.items():
            if FormSetClass.parent_formset_name is None:
                formset_instance = FormSetClass(
                    instance=getattr(self, 'object', None),
                    prefix=name,
                )
                formset_instance.model_name = name
                formset_instance.verbose_name = FormSetClass.model._meta.verbose_name_plural

                config = config_map.get(name, {})
                if config.get('mode') == 'sequential':
                    self._attach_sequential_meta(formset_instance, name, config)
                else:
                    formset_instance.sequential_mode = False
                    for form in formset_instance.forms:
                        form.children = []
                        self._apply_widget_styling_to_form(form)

                self.formset_instances[name] = formset_instance
                self.all_formsets_by_prefix[name] = formset_instance

        for name, FormSetClass in formsets.items():
            parent_name = FormSetClass.parent_formset_name
            if parent_name and parent_name in self.formset_instances:
                parent_formset = self.formset_instances[parent_name]
                if getattr(parent_formset, 'sequential_mode', False):
                    continue
                for i, parent_form in enumerate(parent_formset.forms):
                    prefix = f'{parent_name}-{i}-{name}'
                    child_formset = FormSetClass(
                        instance=parent_form.instance,
                        prefix=prefix,
                        parent_form=parent_form,
                    )
                    child_formset.model_name = name
                    child_formset.verbose_name = FormSetClass.model._meta.verbose_name_plural
                    for form in child_formset.forms:
                        form.children = []
                        self._apply_widget_styling_to_form(form)
                    parent_form.children.append(child_formset)
                    self.all_formsets_by_prefix[prefix] = child_formset

    def bind_formsets(self, request):
        self.formset_instances = {}
        formsets = self.get_formsets()
        config_map = {c.get('prefix', c['model']._meta.model_name): c for c in (self.inline_formsets or [])}

        for name, FormSetClass in formsets.items():
            if FormSetClass.parent_formset_name is None:
                config = config_map.get(name, {})

                if config.get('mode') == 'sequential':
                    # Not bound to main form POST — initialize unbound for display
                    formset_instance = FormSetClass(
                        instance=getattr(self, 'object', None),
                        prefix=name,
                    )
                    formset_instance.model_name = name
                    formset_instance.verbose_name = FormSetClass.model._meta.verbose_name_plural
                    self._attach_sequential_meta(formset_instance, name, config)
                    self.formset_instances[name] = formset_instance
                    continue

                formset_instance = FormSetClass(
                    request.POST,
                    request.FILES,
                    instance=getattr(self, 'object', None),
                    prefix=name,
                )
                formset_instance.verbose_name = FormSetClass.model._meta.verbose_name_plural
                formset_instance.sequential_mode = False
                for form in formset_instance.forms:
                    form.children = []
                    self._apply_widget_styling_to_form(form)
                self.formset_instances[name] = formset_instance

        for name, FormSetClass in formsets.items():
            parent_name = FormSetClass.parent_formset_name
            if parent_name and parent_name in self.formset_instances:
                parent_formset = self.formset_instances[parent_name]
                if getattr(parent_formset, 'sequential_mode', False):
                    continue
                for i, parent_form in enumerate(parent_formset.forms):
                    child_formset = FormSetClass(
                        request.POST,
                        request.FILES,
                        instance=parent_form.instance,
                        prefix=f'{parent_name}-{i}-{name}',
                        parent_form=parent_form,
                    )
                    child_formset.verbose_name = FormSetClass.model._meta.verbose_name_plural
                    for form in child_formset.forms:
                        form.children = []
                        self._apply_widget_styling_to_form(form)
                    parent_form.children.append(child_formset)

    def add_formset(self, formset_class_name, prefix, form_index):
        formsets = self.get_formsets()
        FormSetClass = formsets.get(formset_class_name)

        formset_instance = FormSetClass(prefix=prefix)
        empty_form = formset_instance.empty_form

        empty_form.prefix = f'{prefix}-{form_index}'
        empty_form.children = []
        self._apply_widget_styling_to_form(empty_form)

        for name, ChildFormSetClass in formsets.items():
            if ChildFormSetClass.parent_formset_name == formset_class_name:
                child_prefix = f'{prefix}-{form_index}-{name}'
                child_formset = ChildFormSetClass(
                    instance=empty_form.instance,
                    prefix=child_prefix,
                    queryset=ChildFormSetClass.model.objects.none(),
                )
                child_formset.model_name = name
                for form in child_formset.forms:
                    form.children = []
                empty_form.children.append(child_formset)

        return empty_form

    def are_formsets_valid(self):
        valid = True
        stack = list(self.formset_instances.values())
        while stack:
            formset = stack.pop()
            if getattr(formset, 'sequential_mode', False):
                continue
            valid = formset.is_valid() and valid
            for form in formset.forms:
                if hasattr(form, 'children'):
                    stack.extend(form.children)
        return valid

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        if self.parent_view and hasattr(self.parent_view, 'get_form_kwargs'):
            self.parent_view.request = self.request
            parent_kwargs = self.parent_view.get_form_kwargs()
            kwargs.update(parent_kwargs)
        return kwargs

    def _apply_widget_styling_to_form(self, form):
        from django import forms as django_forms
        from django.conf import settings
        from orange_sherbert.defaults import DEFAULT_FIELD_WIDGETS
        from orange_sherbert import widgets as orange_widgets

        global_widgets = getattr(settings, 'ORANGE_SHERBERT_FIELD_WIDGETS', DEFAULT_FIELD_WIDGETS)
        view_widgets = getattr(self.parent_view, 'field_widgets', {}) if self.parent_view else {}

        for field_name, field in form.fields.items():
            widget_config = None

            if field_name in view_widgets:
                widget_config = view_widgets[field_name]
            else:
                field_type = field.__class__.__name__
                if field_type in global_widgets:
                    widget_config = global_widgets[field_type]

            if widget_config:
                widget_class_name, css_classes, extra_attrs = widget_config

                widget_class = getattr(orange_widgets, widget_class_name, None)
                if not widget_class:
                    widget_class = getattr(django_forms, widget_class_name, None)

                if not widget_class and '.' in widget_class_name:
                    try:
                        from importlib import import_module
                        module_path, class_name = widget_class_name.rsplit('.', 1)
                        module = import_module(module_path)
                        widget_class = getattr(module, class_name, None)
                    except (ImportError, AttributeError, ValueError):
                        pass

                if widget_class:
                    attrs = {'class': css_classes}
                    attrs.update({k: v for k, v in extra_attrs.items() if k != 'type'})

                    current_widget = field.widget.__class__.__name__
                    current_widget_class = field.widget.__class__

                    if current_widget_class == widget_class:
                        existing_classes = field.widget.attrs.get('class', '')
                        if existing_classes:
                            existing_set = set(existing_classes.split())
                            new_set = set(css_classes.split())
                            combined = existing_set | new_set
                            field.widget.attrs['class'] = ' '.join(sorted(combined))
                        else:
                            field.widget.attrs['class'] = css_classes
                    elif current_widget in ('TextInput', 'Textarea', 'Select', 'SelectMultiple', 'NumberInput',
                                         'DateInput', 'TimeInput', 'DateTimeInput', 'CheckboxInput'):
                        if current_widget in ('Select', 'SelectMultiple', 'CheckboxInput'):
                            field.widget.attrs.update(attrs)
                        else:
                            field.widget = widget_class(attrs=attrs)
                    else:
                        existing_classes = field.widget.attrs.get('class', '')
                        if existing_classes:
                            existing_set = set(existing_classes.split())
                            new_set = set(css_classes.split())
                            combined = existing_set | new_set
                            field.widget.attrs['class'] = ' '.join(sorted(combined))
                        else:
                            field.widget.attrs['class'] = css_classes

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        self._apply_widget_styling_to_form(form)
        if self.parent_view and hasattr(self.parent_view, 'get_form'):
            form = self.parent_view.get_form(form, self.request)
        return form

    def save_formsets(self):
        for formset in self.formset_instances.values():
            if getattr(formset, 'sequential_mode', False):
                continue
            formset.instance = self.object

        stack = [f for f in self.formset_instances.values() if not getattr(f, 'sequential_mode', False)]
        while stack:
            formset = stack.pop(0)
            formset.save()
            for form in formset.forms:
                if hasattr(form, 'children'):
                    for child in form.children:
                        child.instance = form.instance
                        stack.append(child)

    def get_queryset(self, **kwargs):
        queryset = super().get_queryset()

        if self.parent_view and hasattr(self.parent_view, 'get_queryset'):
            queryset = self.parent_view.get_queryset(queryset, self.request)

        filter_fields = self.filter_fields
        if filter_fields:
            for field in filter_fields:
                field_name = field
                field_value = self.request.GET.get(field_name)
                if field_value:
                    queryset = queryset.filter(**{field_name: field_value})

        search_query = self.request.GET.get('search', '').strip()
        search_fields = self.search_fields
        if search_query and search_fields:
            q_objects = Q()
            for field in search_fields:
                q_objects |= Q(**{f'{field}__icontains': search_query})
            queryset = queryset.filter(q_objects)

        sort_by = self.request.GET.get('sort_by')
        sort_dir = self.request.GET.get('sort_dir', 'asc')
        if sort_by:
            property_field_map = getattr(self, 'property_field_map', {})
            db_field = property_field_map.get(sort_by, sort_by)
            order_field = f'-{db_field}' if sort_dir == 'desc' else db_field
            queryset = queryset.order_by(order_field)

        return queryset

    def _map_to_fields(self, raw):
        """Normalize a per-field config (dict or positional list/tuple) into a
        {field_name: value} dict keyed by the list view's display fields."""
        if isinstance(raw, (list, tuple)):
            field_names = list(self.fields.keys()) if isinstance(self.fields, dict) else list(self.fields)
            return {field_names[i]: raw[i] for i in range(min(len(field_names), len(raw)))}
        return raw or {}

    def _build_form_columns(self, form):
        """Resolve form_layout (a list of field-name lists, one per column)
        into columns of bound fields, plus any visible form fields not
        mentioned in the layout (rendered full-width below the columns).

        Names absent from the form but valid on the model or declared in
        form_fields are skipped silently — they were removed for this user
        (restricted_fields) or this view. Unknown names raise, so typos
        fail loudly instead of dropping a field."""
        from django.core.exceptions import ImproperlyConfigured

        declared = self.parent_view.form_fields if self.parent_view else {}
        model_field_names = {f.name for f in self.model._meta.get_fields()}

        columns = []
        placed = set()
        for column in self.form_layout:
            bound_column = []
            for name in column:
                name = self.property_field_map.get(name, name)
                if name in form.fields:
                    placed.add(name)
                    bound_column.append(form[name])
                elif name in model_field_names or name in declared:
                    continue
                else:
                    raise ImproperlyConfigured(
                        f"form_layout references unknown field '{name}' "
                        f"for {self.model._meta.label}"
                    )
            columns.append(bound_column)

        leftover = [
            form[name] for name in form.fields
            if name not in placed and not form[name].is_hidden
        ]
        return columns, leftover

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        meta = self.model._meta
        object_data = []

        if 'object_list' in context:
            for obj in context['object_list']:
                field_tuples = []
                for field_name, verbose_name in self.fields.items():
                    value = getattr(obj, field_name, '')
                    field_tuples.append((field_name, verbose_name, value))

                object_data.append({
                    'object': obj,
                    'fields': field_tuples,
                })

        url_namespace = f'{self.url_namespace}:' if self.url_namespace else ''

        field_widths_map = self._map_to_fields(self.field_widths)
        cell_css_map = self._map_to_fields(self.cell_css)

        context.update({
            'model_name': meta.model_name,
            'verbose_name': meta.verbose_name,
            'verbose_name_plural': meta.verbose_name_plural,
            'fields': self.fields,
            'object_data': object_data,
            'filter_fields': self.filter_fields,
            'search_fields': self.search_fields,
            'search_query': self.request.GET.get('search', ''),
            'list_query_params': self.request.session.get(f'list_query_params_{meta.model_name}', ''),
            'extra_actions': self.extra_actions,
            'url_namespace': url_namespace,
            'field_widths': field_widths_map,
            'cell_css': cell_css_map,
            'view_type': self.view_type,
            'side_cards_template': getattr(self.parent_view, 'side_cards_template', None)
                if self.view_type != 'create' or getattr(self.parent_view, 'side_cards_on_create', True)
                else None,
            'top_cards_template': getattr(self.parent_view, 'top_cards_template', None)
                if self.view_type != 'create' or getattr(self.parent_view, 'top_cards_on_create', True)
                else None,
            'show_sequential': self.view_type != 'create' or getattr(self.parent_view, 'sequential_on_create', False),
        })

        if self.view_type == 'detail' and 'object' in context:
            obj = context['object']
            detail_fields = []
            for field_name, verbose_name in self.fields.items():
                value = getattr(obj, field_name, '')
                detail_fields.append((field_name, verbose_name, value))
            context['detail_fields'] = detail_fields

            if self.inline_formsets:
                related_items = []
                for config in self.inline_formsets:
                    if not config.get('nested_under'):
                        model = config['model']
                        prefix = config.get('prefix', model._meta.model_name)

                        fk_field = None
                        for field in model._meta.fields:
                            if field.related_model == self.model:
                                fk_field = field.name
                                break

                        if fk_field:
                            filter_kwargs = {fk_field: obj}
                            queryset_filter = config.get('queryset_filter', {})
                            if queryset_filter:
                                filter_kwargs.update(queryset_filter)
                            related_objs = model.objects.filter(**filter_kwargs)

                            display_fields = config.get('fields', '__all__')
                            if display_fields == '__all__':
                                display_fields = [f.name for f in model._meta.fields if not f.primary_key and f.name != fk_field]

                            items_data = []
                            for related_obj in related_objs:
                                item_fields = []
                                for field_name in display_fields:
                                    field = model._meta.get_field(field_name)
                                    value = getattr(related_obj, field_name, '')
                                    item_fields.append((field.verbose_name, value))
                                items_data.append({
                                    'object': related_obj,
                                    'fields': item_fields,
                                })

                            related_items.append({
                                'prefix': prefix,
                                'verbose_name': model._meta.verbose_name,
                                'verbose_name_plural': model._meta.verbose_name_plural,
                                'items': items_data,
                            })

                context['related_items'] = related_items

        if self.view_type in ('create', 'update') and self.form_layout and context.get('form'):
            columns, leftover = self._build_form_columns(context['form'])
            context['form_columns'] = columns
            context['form_leftover_fields'] = leftover

        if self.view_type in ('create', 'update') and self.inline_formsets:
            if not hasattr(self, 'formset_instances'):
                self.init_formsets()
            context['formsets'] = self.formset_instances

        if self.parent_view and hasattr(self.parent_view, 'get_context_data'):
            context = self.parent_view.get_context_data(context, self.request)

        return context

    def get_success_url(self):
        if (
            self.view_type == 'create'
            and self.parent_view
            and hasattr(self.parent_view, 'get_post_create_url')
            and getattr(self, 'object', None)
        ):
            url = self.parent_view.get_post_create_url(self.object)
            if url:
                return url

        model_name = self.model._meta.model_name
        url_name = f'{self.url_namespace}:{model_name}-list' if self.url_namespace else f'{model_name}-list'
        base_url = reverse(url_name)

        session_key = f'list_query_params_{model_name}'
        query_params = self.request.session.get(session_key, '')
        if query_params:
            return f'{base_url}?{query_params}'
        return base_url

    def get(self, request, *args, **kwargs):
        if self.view_type == 'create':
            self.object = None
        elif self.view_type == 'update':
            self.object = self.get_object()

        if self.view_type in ('create', 'update', 'delete', 'detail'):
            referer = request.META.get('HTTP_REFERER', '')
            if referer and '?' in referer:
                query_string = referer.split('?', 1)[1]
                session_key = f'list_query_params_{self.model._meta.model_name}'
                request.session[session_key] = query_string

        if self.inline_formsets:
            self.init_formsets()
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        if self.view_type == 'create':
            self.object = None
        elif self.view_type in ('update', 'delete'):
            self.object = self.get_object()

        if self.view_type == 'delete':
            return super().post(request, *args, **kwargs)

        if self.inline_formsets:
            self.init_formsets()

        if request.htmx:
            if request.POST.get('formset_sequential_save'):
                return self._handle_sequential_save(request)
            if request.POST.get('formset_sequential_delete'):
                return self._handle_sequential_delete(request)

            formset_class = request.POST.get('formset_class')
            prefix = request.POST.get('prefix')
            try:
                form_index = int(request.POST.get('form_index', 0))
            except (TypeError, ValueError):
                return HttpResponse("Invalid form_index", status=400)
            form = self.add_formset(formset_class, prefix, form_index)
            if form:
                html = render_to_string(
                    'orange_sherbert/includes/form.html',
                    {'form': form},
                    request=request,
                )
                html = html.replace('__prefix__', str(form_index))
                return HttpResponse(html)
            return HttpResponse(f"Formset class '{formset_class}' not found", status=400)

        form = self.get_form()
        if self.inline_formsets:
            self.bind_formsets(request)
            if form.is_valid() and self.are_formsets_valid():
                return self.form_valid(form)
        else:
            if form.is_valid():
                return self.form_valid(form)
        return self.form_invalid(form)

    def form_valid(self, form):
        if self.parent_view and hasattr(self.parent_view, 'form_valid'):
            self.parent_view.form_valid(form)

        if hasattr(form, 'save'):
            with transaction.atomic():
                self.object = form.save()
                if self.inline_formsets:
                    self.save_formsets()

                if self.parent_view and hasattr(self.parent_view, 'post_save'):
                    self.parent_view.post_save(self.object, self.request)

        return super().form_valid(form)

class _CRUDListView(_CRUDMixin, ListView):
    template_name = 'orange_sherbert/list.html'

class _CRUDDetailView(_CRUDMixin, DetailView):
    template_name = 'orange_sherbert/detail.html'

class _CRUDCreateView(_CRUDMixin, CreateView):
    template_name = 'orange_sherbert/create.html'

class _CRUDUpdateView(_CRUDMixin, UpdateView):
    template_name = 'orange_sherbert/update.html'

class _CRUDDeleteView(_CRUDMixin, DeleteView):
    template_name = 'orange_sherbert/delete.html'

    def get_context_data(self, **kwargs):
        if not hasattr(self, 'object') or not self.object:
            self.object = self.get_object()
        return super().get_context_data(**kwargs)

    def form_valid(self, form):
        if not hasattr(self, 'object') or not self.object:
            self.object = self.get_object()
        return DeleteView.form_valid(self, form)


class CRUDView(View):
    model: type[Model]
    enforce_model_permissions = False
    fields = []
    form_fields = {}
    extra_actions = []
    restricted_fields = {}
    filter_fields = {}
    search_fields = []
    property_field_map = {}
    inline_formsets = []
    field_widgets = {}
    field_widths = {}
    cell_css = {}
    form_layout = None
    view_type = None
    url_namespace = None
    url_prefix = None
    path_converter = 'int'
    list_template_name = 'orange_sherbert/list.html'
    detail_template_name = 'orange_sherbert/detail.html'
    create_template_name = 'orange_sherbert/create.html'
    update_template_name = 'orange_sherbert/update.html'
    delete_template_name = 'orange_sherbert/delete.html'
    side_cards_template = None
    top_cards_template = None
    side_cards_on_create = True
    top_cards_on_create = True
    sequential_on_create = False

    def dispatch(self, request, *args, **kwargs):
        view_type = getattr(self, 'view_type', 'list')

        permission_map = {
            'list': 'view',
            'detail': 'view',
            'create': 'add',
            'update': 'change',
            'delete': 'delete',
        }
        view_classes = {
            'list': _CRUDListView,
            'detail': _CRUDDetailView,
            'create': _CRUDCreateView,
            'update': _CRUDUpdateView,
            'delete': _CRUDDeleteView,
        }
        view_class = view_classes[view_type]

        action = permission_map.get(view_type, 'view')
        app_label = self.model._meta.app_label
        model_name = self.model._meta.model_name
        permission = f'{app_label}.{action}_{model_name}'

        if self.fields == '__all__':
            instance_fields = {f.name: f.verbose_name for f in self.model._meta.fields if not f.primary_key}
        else:
            instance_fields = self.fields.copy() if isinstance(self.fields, dict) else self.fields

        instance_form_fields = self.form_fields.copy() if isinstance(self.form_fields, dict) else self.form_fields

        if self.restricted_fields:
            for field, required_permission in self.restricted_fields.items():
                if field in instance_fields and not request.user.has_perm(required_permission):
                    del instance_fields[field]
                if instance_form_fields and field in instance_form_fields and not request.user.has_perm(required_permission):
                    del instance_form_fields[field]

        if self.enforce_model_permissions and not request.user.has_perm(permission):
            return HttpResponseForbidden("You do not have permission to perform this action.")

        form_fields = instance_form_fields if instance_form_fields else instance_fields
        if view_type in ('create', 'update') and self.property_field_map:
            resolved_form_fields = {}
            for k, v in form_fields.items():
                if k in self.property_field_map:
                    db_field = self.property_field_map[k]
                    resolved_form_fields[db_field] = v
                else:
                    resolved_form_fields[k] = v
            form_fields = resolved_form_fields

        has_custom_form = hasattr(self, 'form_class') and self.form_class is not None

        view_kwargs = {
            'model': self.model,
            'filter_fields': self.filter_fields,
            'search_fields': self.search_fields,
            'extra_actions': self.extra_actions,
            'property_field_map': self.property_field_map,
            'view_type': view_type,
            'form_fields': instance_form_fields,
            'url_namespace': self.url_namespace,
            'inline_formsets': self.inline_formsets,
            'parent_view': self,
            'field_widths': self.field_widths,
            'cell_css': self.cell_css,
            'form_layout': self.form_layout,
        }

        if has_custom_form and view_type in ('create', 'update'):
            view_kwargs['form_class'] = self.form_class
        else:
            view_kwargs['fields'] = form_fields if view_type in ('create', 'update', 'detail') else instance_fields

        if view_type == 'list':
            view_kwargs['template_name'] = self.list_template_name
        elif view_type == 'detail':
            view_kwargs['template_name'] = self.detail_template_name
        elif view_type == 'create':
            view_kwargs['template_name'] = self.create_template_name
        elif view_type == 'update':
            view_kwargs['template_name'] = self.update_template_name
        elif view_type == 'delete':
            view_kwargs['template_name'] = self.delete_template_name

        view = view_class.as_view(**view_kwargs)
        return view(request, *args, **kwargs)

    @classmethod
    def get_model_name(cls):
        if cls.model is None:
            raise ValueError("model attribute must be set")
        return cls.model._meta.model_name

    @classmethod
    def get_urls(cls):
        model_name = cls.get_model_name()

        url_base = cls.url_prefix if cls.url_prefix else model_name
        name_base = cls.url_prefix if cls.url_prefix else model_name

        pk_type = cls.path_converter

        urls = [
            path(f'{url_base}/', cls.as_view(view_type='list'), name=f'{name_base}-list'),
            path(f'{url_base}/create/', cls.as_view(view_type='create'), name=f'{name_base}-create'),
            path(f'{url_base}/<{pk_type}:pk>/', cls.as_view(view_type='detail'), name=f'{name_base}-detail'),
            path(f'{url_base}/<{pk_type}:pk>/update/', cls.as_view(view_type='update'), name=f'{name_base}-update'),
            path(f'{url_base}/<{pk_type}:pk>/delete/', cls.as_view(view_type='delete'), name=f'{name_base}-delete'),
        ]

        if cls.extra_actions:
            for action in cls.extra_actions:
                action_name = action['name']
                view_class = action['view']

                url_name = f"{name_base}-{action_name}"
                url_path = f'{url_base}/<{pk_type}:pk>/{action_name}/'
                urls.append(path(url_path, view_class.as_view(), name=url_name))

        return urls
