from django.views.generic import DetailView
from django.views.generic import ListView
from django.views.generic import CreateView
from django.views.generic import UpdateView
from django.views.generic import DeleteView
from django.views import View
from django.urls import path, reverse
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

    def get_formsets(self):
        formsets = {}

        if self.inline_formsets:
            for config in self.inline_formsets:
                # Use custom prefix if provided, otherwise use model name
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
    
    def init_formsets(self):
        self.formset_instances = {}
        self.all_formsets_by_prefix = {}
        formsets = self.get_formsets()
        
        for name, FormSetClass in formsets.items():
            if FormSetClass.parent_formset_name is None:
                formset_instance = FormSetClass(
                    instance=getattr(self, 'object', None),
                    prefix=name,
                )
                formset_instance.model_name = name
                formset_instance.verbose_name = FormSetClass.model._meta.verbose_name_plural
                for form in formset_instance.forms:
                    form.children = []
                    self._apply_widget_styling_to_form(form)
                self.formset_instances[name] = formset_instance
                self.all_formsets_by_prefix[name] = formset_instance
        
        for name, FormSetClass in formsets.items():
            parent_name = FormSetClass.parent_formset_name
            if parent_name and parent_name in self.formset_instances:
                parent_formset = self.formset_instances[parent_name]
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
        
        for name, FormSetClass in formsets.items():
            if FormSetClass.parent_formset_name is None:
                formset_instance = FormSetClass(
                    request.POST,
                    request.FILES,
                    instance=getattr(self, 'object', None),
                    prefix=name,
                )
                formset_instance.verbose_name = FormSetClass.model._meta.verbose_name_plural
                for form in formset_instance.forms:
                    form.children = []
                    self._apply_widget_styling_to_form(form)
                self.formset_instances[name] = formset_instance
        
        for name, FormSetClass in formsets.items():
            parent_name = FormSetClass.parent_formset_name
            if parent_name and parent_name in self.formset_instances:
                parent_formset = self.formset_instances[parent_name]
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
            valid = formset.is_valid() and valid
            for form in formset.forms:
                if hasattr(form, 'children'):
                    stack.extend(form.children)
        return valid
    
    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        # Call parent_view's get_form_kwargs if it exists
        if self.parent_view and hasattr(self.parent_view, 'get_form_kwargs'):
            # Set request on parent_view so it can access self.request
            self.parent_view.request = self.request
            parent_kwargs = self.parent_view.get_form_kwargs()
            kwargs.update(parent_kwargs)
        return kwargs
    
    def _apply_widget_styling_to_form(self, form):
        """Apply global and view-level widget styling to a form (including inline formset forms)"""
        from django import forms as django_forms
        from django.conf import settings
        from orange_sherbert.defaults import DEFAULT_FIELD_WIDGETS
        from orange_sherbert import widgets as orange_widgets
        
        # Get global widget configuration (field-type-based)
        global_widgets = getattr(settings, 'ORANGE_SHERBERT_FIELD_WIDGETS', DEFAULT_FIELD_WIDGETS)
        
        # Get view-level widget configuration (field-name-based)
        view_widgets = getattr(self.parent_view, 'field_widgets', {}) if self.parent_view else {}
        
        # Apply widget configuration
        for field_name, field in form.fields.items():
            widget_config = None
            
            # Check for field-name-based override first (most specific)
            if field_name in view_widgets:
                widget_config = view_widgets[field_name]
            else:
                # Fall back to field-type-based global config
                field_type = field.__class__.__name__
                if field_type in global_widgets:
                    widget_config = global_widgets[field_type]
            
            # Apply widget configuration if found
            if widget_config:
                widget_class_name, css_classes, extra_attrs = widget_config
                
                # Get the widget class - try orange_sherbert widgets first, then django.forms
                widget_class = getattr(orange_widgets, widget_class_name, None)
                if not widget_class:
                    widget_class = getattr(django_forms, widget_class_name, None)
                
                # If still not found, try importing as a fully qualified path
                if not widget_class and '.' in widget_class_name:
                    try:
                        from importlib import import_module
                        module_path, class_name = widget_class_name.rsplit('.', 1)
                        module = import_module(module_path)
                        widget_class = getattr(module, class_name, None)
                    except (ImportError, AttributeError, ValueError):
                        pass
                
                if widget_class:
                    # Build widget attributes (remove 'type' from attrs since it's set by widget class)
                    attrs = {'class': css_classes}
                    attrs.update({k: v for k, v in extra_attrs.items() if k != 'type'})
                    
                    # Only replace widget if it's still the default
                    # This preserves custom widgets defined in form classes
                    current_widget = field.widget.__class__.__name__
                    current_widget_class = field.widget.__class__
                    
                    # Check if current widget matches the configured widget class
                    # If so, just merge CSS classes instead of replacing
                    if current_widget_class == widget_class:
                        # Same widget class - just merge CSS classes
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
                        # For Select/SelectMultiple/CheckboxInput, preserve functionality by just updating attrs
                        if current_widget in ('Select', 'SelectMultiple', 'CheckboxInput'):
                            # Just update the attrs on the existing widget instead of replacing it
                            field.widget.attrs.update(attrs)
                        else:
                            # For other widgets, safe to replace
                            field.widget = widget_class(attrs=attrs)
                    else:
                        # Widget was explicitly set (custom widget), merge CSS classes
                        existing_classes = field.widget.attrs.get('class', '')
                        if existing_classes:
                            # Merge with existing classes, avoiding duplicates
                            existing_set = set(existing_classes.split())
                            new_set = set(css_classes.split())
                            combined = existing_set | new_set
                            field.widget.attrs['class'] = ' '.join(sorted(combined))
                        else:
                            # No existing classes, just add ours
                            field.widget.attrs['class'] = css_classes
    
    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        
        # Apply widget styling to the main form
        self._apply_widget_styling_to_form(form)
        
        # Call parent_view's get_form if it exists
        if self.parent_view and hasattr(self.parent_view, 'get_form'):
            form = self.parent_view.get_form(form, self.request)
        
        return form

    def save_formsets(self):
        for formset in self.formset_instances.values():
            formset.instance = self.object
        
        stack = list(self.formset_instances.values())
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
        
        # Call parent_view's get_queryset if it exists
        if self.parent_view and hasattr(self.parent_view, 'get_queryset'):
            queryset = self.parent_view.get_queryset(queryset, self.request)
        
        filter_fields = self.filter_fields
        if filter_fields:
            for field in filter_fields:
                field_name = field if isinstance(filter_fields, list) else field
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

        # Resolve field_widths into a dict keyed by field name
        raw_widths = self.field_widths
        if isinstance(raw_widths, (list, tuple)):
            field_names = list(self.fields.keys()) if isinstance(self.fields, dict) else list(self.fields)
            field_widths_map = {field_names[i]: raw_widths[i] for i in range(min(len(field_names), len(raw_widths)))}
        else:
            field_widths_map = raw_widths

        context.update({
            'model_name': meta.model_name,
            'verbose_name': meta.verbose_name,
            'verbose_name_plural': meta.verbose_name_plural,
            'fields': self.fields,
            'object_data': object_data,
            'filter_fields': self.filter_fields,
            'search_fields': self.search_fields,
            'search_query': self.request.GET.get('search', ''),
            'extra_actions': self.extra_actions,
            'url_namespace': url_namespace,
            'field_widths': field_widths_map,
        })
        
        if self.view_type == 'detail' and 'object' in context:
            obj = context['object']
            detail_fields = []
            for field_name, verbose_name in self.fields.items():
                value = getattr(obj, field_name, '')
                detail_fields.append((field_name, verbose_name, value))
            context['detail_fields'] = detail_fields
            
            # Add related items from inline formsets for detail view
            if self.inline_formsets:
                related_items = []
                for config in self.inline_formsets:
                    # Only show top-level formsets (not nested ones)
                    if not config.get('nested_under'):
                        model = config['model']
                        prefix = config.get('prefix', model._meta.model_name)
                        
                        # Get the foreign key field that relates to the parent model
                        fk_field = None
                        for field in model._meta.fields:
                            if field.related_model == self.model:
                                fk_field = field.name
                                break
                        
                        if fk_field:
                            # Fetch related objects with queryset filter if provided
                            filter_kwargs = {fk_field: obj}
                            queryset_filter = config.get('queryset_filter', {})
                            if queryset_filter:
                                filter_kwargs.update(queryset_filter)
                            related_objs = model.objects.filter(**filter_kwargs)
                            
                            # Get fields to display
                            display_fields = config.get('fields', '__all__')
                            if display_fields == '__all__':
                                display_fields = [f.name for f in model._meta.fields if not f.primary_key and f.name != fk_field]
                            
                            # Build data structure for template
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
        
        if self.view_type in ('create', 'update') and self.inline_formsets:
            if not hasattr(self, 'formset_instances'):
                self.init_formsets()
            context['formsets'] = self.formset_instances
        
        if self.parent_view and hasattr(self.parent_view, 'get_context_data'):
            context = self.parent_view.get_context_data(context, self.request)
        
        return context
    
    def get_success_url(self):
        model_name = self.model._meta.model_name
        url_name = f'{self.url_namespace}:{model_name}-list' if self.url_namespace else f'{model_name}-list'
        base_url = reverse(url_name)
        
        # Preserve query parameters from the session if they exist
        query_params = self.request.session.get('list_query_params', '')
        if query_params:
            return f'{base_url}?{query_params}'
        return base_url

    def get(self, request, *args, **kwargs):
        if self.view_type == 'create':
            self.object = None
        elif self.view_type == 'update':
            self.object = self.get_object()
        
        # Store query parameters from referrer for create/update/delete views
        if self.view_type in ('create', 'update', 'delete'):
            referer = request.META.get('HTTP_REFERER', '')
            if referer and '?' in referer:
                query_string = referer.split('?', 1)[1]
                request.session['list_query_params'] = query_string
        
        if self.inline_formsets:
            self.init_formsets()
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        if self.view_type == 'create':
            self.object = None
        elif self.view_type in ('update', 'delete'):
            self.object = self.get_object()
        
        # For delete views, skip form handling and let DeleteView handle it
        if self.view_type == 'delete':
            return super().post(request, *args, **kwargs)
        
        if self.inline_formsets:
            self.init_formsets()
        
        if request.htmx:
            formset_class = request.POST.get('formset_class')
            prefix = request.POST.get('prefix')
            form_index = int(request.POST.get('form_index', 0))
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
        # Call parent_view's form_valid before save if it exists
        if self.parent_view and hasattr(self.parent_view, 'form_valid'):
            self.parent_view.form_valid(form)
        
        # Only save form if it has a save method (delete forms don't)
        if hasattr(form, 'save'):
            self.object = form.save()
            if self.inline_formsets:
                self.save_formsets()
            
            # Call parent_view's post_save if it exists (for M2M relations, etc.)
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
        # Ensure self.object is set before calling parent's get_context_data
        if not hasattr(self, 'object') or not self.object:
            self.object = self.get_object()
        return super().get_context_data(**kwargs)
    
    def form_valid(self, form):
        # Set self.object before calling parent's form_valid
        # DeleteView needs this to delete the object
        if not hasattr(self, 'object') or not self.object:
            self.object = self.get_object()
        # Call the actual delete logic from DeleteView
        return DeleteView.form_valid(self, form)


class CRUDView(View):
    model: type[Model]
    enforce_model_permissions = False
    fields = []
    form_fields = []
    extra_actions = []
    restricted_fields = []
    filter_fields = {}
    search_fields = []
    property_field_map = {}
    inline_formsets = []
    field_widgets = {}  # View-level widget configuration: {'field_name': ('WidgetClass', 'css classes', {attrs})}
    field_widths = {}   # Column width percentages for list view: {'field_name': 25} or [10, 5, 25, 35]
    view_type = None
    url_namespace = None
    url_prefix = None  # Custom URL prefix to override model name (e.g., 'admin-user' instead of 'user')
    path_converter = 'int'  # 'int', 'uuid', 'slug', etc.
    list_template_name = 'orange_sherbert/list.html'
    detail_template_name = 'orange_sherbert/detail.html'
    create_template_name = 'orange_sherbert/create.html'
    update_template_name = 'orange_sherbert/update.html'
    delete_template_name = 'orange_sherbert/delete.html'

    def __init_subclass__(cls, abstract: bool = False, **kwargs):
        super().__init_subclass__(**kwargs)
        if not abstract and not hasattr(cls, 'model'):
            raise TypeError(f"{cls.__name__} must define a model attribute")

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
        
        # Create instance-level copies of fields to avoid mutating class-level attributes
        if self.fields == '__all__':
            instance_fields = {f.name: f.verbose_name for f in self.model._meta.fields if not f.primary_key}
        else:
            instance_fields = self.fields.copy() if isinstance(self.fields, dict) else self.fields
        
        instance_form_fields = self.form_fields.copy() if isinstance(self.form_fields, dict) else self.form_fields
        
        # Filter out restricted fields based on user permissions
        if self.restricted_fields:
            for field, required_permission in self.restricted_fields.items():
                if field in instance_fields and not request.user.has_perm(required_permission):
                    del instance_fields[field]
                if instance_form_fields and field in instance_form_fields and not request.user.has_perm(required_permission):
                    del instance_form_fields[field]
 
        if self.enforce_model_permissions and not request.user.has_perm(permission):
            return HttpResponseForbidden("You do not have permission to perform this action.")
        
        # For create/update views, replace properties with their underlying model fields
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
        
        # Check if custom form_class is defined
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
        }
        
        # Only pass fields if no custom form_class (Django doesn't allow both)
        # form_class only applies to create/update views
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
        
        # Use url_prefix if set, otherwise use model_name
        url_base = cls.url_prefix if cls.url_prefix else model_name
        # Use url_prefix for URL names too if set, otherwise use model_name
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