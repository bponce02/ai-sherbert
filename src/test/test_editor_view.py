import pymupdf
import pytest
from django.contrib.auth.models import User
from django.core.files.base import ContentFile

from sherbert_pdf.models import PDFDocument


def make_pdf_bytes(pages=1):
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def owner(db):
    return User.objects.create_user('owner', password='pw')


@pytest.fixture
def pdf_doc(owner, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    doc = PDFDocument(title='Test Doc', user=owner)
    doc.file.save('test.pdf', ContentFile(make_pdf_bytes()), save=True)
    return doc


@pytest.mark.django_db
def test_editor_page_renders_for_owner(client, owner, pdf_doc):
    client.force_login(owner)
    response = client.get(f'/pdf/editor/{pdf_doc.pk}/')
    assert response.status_code == 200
    content = response.content.decode()
    assert 'sherbert-config' in content
    assert 'konva' in content.lower()
    assert pdf_doc.file.url in content


@pytest.mark.django_db
def test_editor_page_404_for_non_owner(client, pdf_doc):
    other = User.objects.create_user('other', password='pw')
    client.force_login(other)
    assert client.get(f'/pdf/editor/{pdf_doc.pk}/').status_code == 404


@pytest.mark.django_db
def test_editor_page_404_for_missing_document(client, owner):
    client.force_login(owner)
    assert client.get('/pdf/editor/99999/').status_code == 404


@pytest.mark.django_db
def test_pdf_index_lists_documents(client, owner, pdf_doc):
    client.force_login(owner)
    response = client.get('/pdfs/')
    assert response.status_code == 200
    content = response.content.decode()
    assert 'Test Doc' in content
    assert f'/pdf/editor/{pdf_doc.pk}/' in content
