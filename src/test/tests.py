import pytest
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission

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


@pytest.fixture
def check_out_perm(db):
    return Permission.objects.get(
        content_type__app_label='example', codename='can_check_out'
    )


def _make_user(username, perms=()):
    user = get_user_model().objects.create_user(username, password='pw')
    for perm in perms:
        user.user_permissions.add(perm)
    # Re-fetch to clear the cached permission set.
    return get_user_model().objects.get(pk=user.pk)


@pytest.mark.django_db
def test_extra_action_without_permission_is_forbidden(client, book, check_out_perm):
    """The 'check-out' action carries permission 'example.can_check_out'.
    A user lacking it gets a 403 from the action URL."""
    client.force_login(_make_user('nobody'))
    response = client.post(f'/book/{book.pk}/check-out/')
    assert response.status_code == 403
    book.refresh_from_db()
    assert book.checked_out is False


@pytest.mark.django_db
def test_extra_action_with_permission_runs(client, book, check_out_perm):
    client.force_login(_make_user('checker', perms=[check_out_perm]))
    response = client.post(f'/book/{book.pk}/check-out/')
    assert response.status_code == 302  # CheckOutView redirects on success
    book.refresh_from_db()
    assert book.checked_out is True


@pytest.mark.django_db
def test_extra_action_without_permission_still_allows_unrestricted_action(client, book):
    """Actions without a 'permission' key (e.g. 'check-in') stay open to all."""
    client.force_login(_make_user('nobody2'))
    response = client.post(f'/book/{book.pk}/check-in/')
    assert response.status_code == 302


@pytest.mark.django_db
def test_extra_action_button_hidden_without_permission(client, book, check_out_perm):
    client.force_login(_make_user('viewer'))
    content = client.get('/book/').content.decode()
    assert '/check-out/' not in content   # gated button hidden
    assert '/check-in/' in content        # ungated button still shown


@pytest.mark.django_db
def test_extra_action_button_shown_with_permission(client, book, check_out_perm):
    client.force_login(_make_user('viewer2', perms=[check_out_perm]))
    content = client.get('/book/').content.decode()
    assert '/check-out/' in content


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