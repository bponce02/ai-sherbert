import pytest
from datetime import date
from decimal import Decimal
from example.models import Author, Book


@pytest.fixture
def author():
    return Author.objects.create(name='Test Author')


@pytest.fixture
def book(author):
    return Book.objects.create(
        title='Test Book',
        author=author,
        isbn='1234567890123',
        price=Decimal('19.99'),
        pub_date=date(2024, 1, 1),
        checked_out=False
    )


@pytest.mark.django_db
@pytest.mark.parametrize('url_template,needs_object', [
    ('/author/', False),
    ('/author/create/', False),
    ('/author/{}/', True),
    ('/author/{}/update/', True),
    ('/author/{}/delete/', True),
    ('/book/', False),
    ('/book/create/', False),
    ('/book/{}/', True),
    ('/book/{}/update/', True),
    ('/book/{}/delete/', True),
])
def test_all_views_return_200(client, author, book, url_template, needs_object):
    """Test that all CRUD views return 200."""
    if needs_object:
        # Use author for author URLs, book for book URLs
        obj = book if url_template.startswith('/book') else author
        url = url_template.format(obj.pk)
    else:
        url = url_template
    
    response = client.get(url)
    assert response.status_code == 200


@pytest.mark.django_db
def test_form_layout_renders_columns(client, author, book):
    """BookCRUDView declares a 3-column form_layout — the grid should render
    on create and update, sized to the number of columns."""
    for url in ('/book/create/', f'/book/{book.pk}/update/'):
        content = client.get(url).content.decode()
        assert 'style="--sherbert-form-cols: 3;"' in content


@pytest.mark.django_db
def test_no_form_layout_renders_flat(client):
    """Views without form_layout keep the original top-down rendering."""
    content = client.get('/author/create/').content.decode()
    assert 'style="--sherbert-form-cols' not in content


def _layout_view(layout):
    from orange_sherbert.view import _CRUDCreateView
    view = _CRUDCreateView()
    view.model = Book
    view.form_layout = layout
    view.property_field_map = {}
    view.parent_view = None
    return view


def _book_form(fields):
    from django.forms import modelform_factory
    return modelform_factory(Book, fields=fields)()


def test_build_form_columns_places_and_collects_leftovers():
    view = _layout_view([['title'], ['author']])
    form = _book_form(['title', 'author', 'isbn', 'price'])
    columns, leftover = view._build_form_columns(form)
    assert [[f.name for f in col] for col in columns] == [['title'], ['author']]
    assert [f.name for f in leftover] == ['isbn', 'price']


def test_build_form_columns_skips_fields_removed_from_form():
    """A layout name that is a real model field but absent from the form
    (e.g. removed by restricted_fields) is skipped, not an error."""
    view = _layout_view([['title', 'ordered_from'], ['author']])
    form = _book_form(['title', 'author'])
    columns, _ = view._build_form_columns(form)
    assert [[f.name for f in col] for col in columns] == [['title'], ['author']]


def test_build_form_columns_raises_on_unknown_field():
    from django.core.exceptions import ImproperlyConfigured
    view = _layout_view([['titel']])
    form = _book_form(['title'])
    with pytest.raises(ImproperlyConfigured):
        view._build_form_columns(form)