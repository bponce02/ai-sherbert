"""<c-sherbert.field> / field_base dispatch and theming."""
import re

import pytest
from django import forms
from django.template.loader import render_to_string


class ProseWidget(forms.Textarea):
    """Stand-in for a third-party editor widget: widget_type == 'prose'."""


class DemoForm(forms.Form):
    title = forms.CharField()
    when = forms.DateField()
    at = forms.TimeField()
    kind = forms.ChoiceField(choices=[('a', 'A'), ('b', 'B')])
    tags = forms.MultipleChoiceField(choices=[('x', 'X'), ('y', 'Y')], widget=forms.CheckboxSelectMultiple)
    mode = forms.ChoiceField(choices=[('r', 'R'), ('s', 'S')], widget=forms.RadioSelect)
    ok = forms.BooleanField(required=False)
    note = forms.CharField(widget=forms.Textarea)
    doc = forms.FileField(required=False)
    body = forms.CharField(widget=ProseWidget(attrs={'data-editor': '1'}))
    secret = forms.CharField(widget=forms.HiddenInput)


THEME = {
    'wrapper': 'form-control',
    'label': 'label',
    'label_text': 'label-text',
    'required': 'text-error',
    'error': 'text-error',
    'help': 'help',
    'input': 'input w-full',
    'input_sm': 'input-sm',
    'input_error': 'input-error',
    'select': 'select w-full',
    'textarea': 'textarea',
    'file': 'file-input',
    'checkbox': 'checkbox',
    'radio': 'radio',
    'choice_list': 'choices',
    'choice_label': 'choice',
    'prose': 'prose-editor',
}


@pytest.fixture
def render(tmp_path, settings):
    """Render a template snippet through the real engine so cotton compiles it.

    Writes the snippet to a temp template dir (also usable for project-level
    control overrides via ``render.dir``) and renders it by name.
    """
    templates = settings.TEMPLATES
    templates[0]['DIRS'] = [str(tmp_path)]
    settings.TEMPLATES = templates  # triggers Django's template-engine reset
    counter = {'n': 0}

    def _render(source, **ctx):
        counter['n'] += 1
        name = f'snippet_{counter["n"]}.html'
        (tmp_path / name).write_text(source)
        return render_to_string(name, ctx)

    _render.dir = tmp_path
    return _render


def control(html, name):
    m = re.search(r'<(input|select|textarea)[^>]*name="%s"[^>]*>' % name, html, re.S)
    assert m, html
    return m.group(0)


def classes_of(tag):
    m = re.search(r'class="([^"]*)"', tag)
    return set(m.group(1).split()) if m else set()


@pytest.fixture
def form():
    return DemoForm()


def test_default_is_themeless_but_dispatches(render, form):
    html = render('<c-sherbert.field :field="form.title" />', form=form)
    assert 'sherbert-field sherbert-field--input' in html
    assert classes_of(control(html, 'title')) == {'sherbert-input'}


def test_date_and_time_get_html5_types(render, form):
    html = render('<c-sherbert.field :field="form.when" /><c-sherbert.field :field="form.at" />', form=form)
    assert 'type="date"' in control(html, 'when')
    assert 'type="time"' in control(html, 'at')


def test_theme_classes_apply_per_control(render, form):
    src = ''.join(
        '<c-sherbert.field_base :field="form.%s" :classes="classes" />' % n
        for n in ['title', 'kind', 'note', 'doc', 'ok']
    )
    html = render(src, form=form, classes=THEME)
    assert {'input', 'w-full'} <= classes_of(control(html, 'title'))
    assert {'select', 'w-full'} <= classes_of(control(html, 'kind'))
    assert 'textarea' in classes_of(control(html, 'note'))
    assert 'file-input' in classes_of(control(html, 'doc'))
    assert 'checkbox' in classes_of(control(html, 'ok'))
    assert 'class="sherbert-field sherbert-field--input form-control"' in html
    assert '<span class="sherbert-required text-error">*</span>' in html


def test_size_error_and_control_class_variants(render):
    bound = DemoForm(data={})  # every required field errors
    html = render(
        '<c-sherbert.field_base :field="form.title" :classes="classes" size="sm" control_class="text-right" />',
        form=bound, classes=THEME,
    )
    assert {'input', 'input-sm', 'input-error', 'text-right'} <= classes_of(control(html, 'title'))
    assert 'sherbert-field--error' in html
    assert '<div class="sherbert-error text-error">' in html


def test_choice_lists_emit_one_styled_input_per_option(render, form):
    html = render(
        '<c-sherbert.field_base :field="form.tags" :classes="classes" />'
        '<c-sherbert.field_base :field="form.mode" :classes="classes" />',
        form=form, classes=THEME,
    )
    boxes = re.findall(r'<input type="checkbox"[^>]*name="tags"[^>]*>', html)
    radios = re.findall(r'<input type="radio"[^>]*name="mode"[^>]*>', html)
    assert len(boxes) == 2 and all('checkbox' in classes_of(b) for b in boxes)
    assert len(radios) == 2 and all('radio' in classes_of(r) for r in radios)
    assert html.count('class="sherbert-choice-list choices"') == 2
    # the wrapper div never carries the option classes
    assert 'div class="checkbox' not in html and 'div class="radio' not in html


def test_unknown_widget_falls_back_to_raw_and_keeps_its_attrs(render, form):
    html = render('<c-sherbert.field :field="form.body" />', form=form)
    tag = control(html, 'body')
    assert 'data-editor="1"' in tag
    assert 'class=' not in tag  # untouched
    assert 'sherbert-field--raw' in html


def test_theme_key_named_after_widget_type_styles_raw_widget(render, form):
    html = render('<c-sherbert.field_base :field="form.body" :classes="classes" />', form=form, classes=THEME)
    assert classes_of(control(html, 'body')) == {'prose-editor'}


def test_kinds_alias_routes_widget_onto_builtin_control(render, form):
    html = render(
        '<c-sherbert.field_base :field="form.body" :classes="classes" :kinds="{\'prose\': \'textarea\'}" />',
        form=form, classes=THEME,
    )
    assert {'textarea', 'sherbert-textarea'} <= classes_of(control(html, 'body'))
    assert 'sherbert-field--textarea' in html


def test_project_control_file_named_after_widget_type_is_picked_up(render, form):
    ctrl = render.dir / 'cotton' / 'sherbert' / 'controls'
    ctrl.mkdir(parents=True)
    (ctrl / 'prose.html').write_text('<c-vars classes="" control_class="" size="" itype="" />'
                                     '<div class="custom-prose">{{ field }}</div>')
    html = render('<c-sherbert.field :field="form.body" />', form=form)
    assert '<div class="custom-prose">' in html
    assert 'sherbert-field--prose' in html


def test_hidden_fields_render_bare(render, form):
    html = render('<c-sherbert.field :field="form.secret" />', form=form)
    assert html.strip().startswith('<input type="hidden"')
    assert 'sherbert-field' not in html


def test_widget_attrs_set_in_python_survive(render, form):
    form.fields['title'].widget.attrs.update({'x-model': 'title', 'class': 'js-hook'})
    html = render('<c-sherbert.field_base :field="form.title" :classes="classes" />', form=form, classes=THEME)
    tag = control(html, 'title')
    assert 'x-model="title"' in tag
    assert {'js-hook', 'input', 'w-full'} <= classes_of(tag)
