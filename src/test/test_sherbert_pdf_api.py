import json

import pymupdf
import pytest
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings

from sherbert_pdf.access import AccessPolicy
from sherbert_pdf.models import PDFAnnotation, PDFDocument


class AllowAllPolicy(AccessPolicy):
    """Test policy: every user can access every document.
    Annotation ownership rules are inherited from AccessPolicy."""

    def can_access_document(self, user, pdf_document):
        return True


ALLOW_ALL_POLICY_PATH = f'{__name__}.AllowAllPolicy'


def make_pdf_bytes():
    doc = pymupdf.open()
    doc.new_page(width=612, height=792)
    data = doc.tobytes()
    doc.close()
    return data


def pen_data(vertices=None, erasures=None):
    data = {
        'vertices': vertices if vertices is not None else [[[10.0, 10.0], [50.0, 50.0], [90.0, 30.0]]],
        'colors': {'stroke': [1.0, 0.0, 0.0]},
        'border': {'width': 2.0},
        'opacity': 1.0,
    }
    if erasures is not None:
        data['erasures'] = erasures
    return data


ANNOTATION_PAYLOADS = {
    'pen': pen_data(),
    'highlighter': {
        'vertices': [[[0.0, 100.0], [200.0, 100.0]]],
        'colors': {'stroke': [1.0, 1.0, 0.0]},
        'border': {'width': 12.0},
        'opacity': 0.3,
    },
    'text': {
        'rect': [50.0, 60.0, 250.0, 120.0],
        'content': 'Hello, world!',
        'colors': {'stroke': [0.0, 0.0, 1.0]},
        'fontSize': 14,
        'fontFamily': 'Arial, sans-serif',
        'fontStyle': 'normal',
    },
    'stamp': {
        'type': 'stamp',
        'x': 100.0,
        'y': 150.0,
        'width': 80.0,
        'height': 40.0,
        'imageUrl': '/static/img/approved-stamp.png',
    },
    'cloud': {
        'vertices': [[[100.0, 100.0, 200.0, 150.0]]],
        'colors': {'stroke': [1.0, 0.0, 0.0]},
        'border': {'width': 2.0},
        'opacity': 1.0,
    },
}


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def user(db):
    return User.objects.create_user('alice', password='pw', first_name='Alice', last_name='Smith')


@pytest.fixture
def other_user(db):
    return User.objects.create_user('bob', password='pw')


@pytest.fixture
def client(client, user):
    client.force_login(user)
    return client


@pytest.fixture
def other_client(other_user):
    c = Client()
    c.force_login(other_user)
    return c


@pytest.fixture
def pdf_doc(user):
    doc = PDFDocument.objects.create(title='Test Doc', user=user, quick_edit=True)
    doc.file.save('test.pdf', ContentFile(make_pdf_bytes()))
    return doc


def create_annotation(client, pdf_doc, annotation_type, data=None, page_number=0):
    payload = {
        'pdf_document_id': pdf_doc.id,
        'annotation': {
            'page_number': page_number,
            'annotation_type': annotation_type,
            'annotation_data': data if data is not None else ANNOTATION_PAYLOADS[annotation_type],
        },
    }
    return client.post('/api/annotations', data=json.dumps(payload), content_type='application/json')


def put_annotation(client, annotation_id, data):
    payload = {'annotation_id': annotation_id, 'annotation_data': data}
    return client.put('/api/annotations', data=json.dumps(payload), content_type='application/json')


def delete_annotation(client, annotation_id):
    payload = {'annotation_id': annotation_id}
    return client.delete('/api/annotations', data=json.dumps(payload), content_type='application/json')


@pytest.mark.django_db
def test_upload_pdf_document(client, user):
    upload = SimpleUploadedFile('upload.pdf', make_pdf_bytes(), content_type='application/pdf')
    response = client.post('/api/pdf-documents?pdf_title=Uploaded+Doc', {'pdf_file': upload})

    assert response.status_code == 201
    body = response.json()
    assert body['title'] == 'Uploaded Doc'
    assert 'pdfs/' in body['file_url']
    assert PDFDocument.objects.get(id=body['id']).user == user


@pytest.mark.django_db
def test_get_pdf_detail(client, pdf_doc):
    response = client.get(f'/api/pdf-documents/{pdf_doc.id}')
    assert response.status_code == 200
    body = response.json()
    assert body == {'id': pdf_doc.id, 'title': 'Test Doc', 'file_url': pdf_doc.file.url}


@pytest.mark.django_db
def test_export_pdf(client, pdf_doc):
    response = client.get(f'/api/pdf-documents/{pdf_doc.id}/export')
    assert response.status_code == 200
    assert response['Content-Type'] == 'application/pdf'
    assert response['Content-Disposition'] == 'inline; filename="Test Doc_exported.pdf"'
    assert response.content.startswith(b'%PDF')


@pytest.mark.django_db
@pytest.mark.parametrize('annotation_type', ['pen', 'highlighter', 'text', 'stamp', 'cloud'])
def test_create_annotation_each_type(client, user, pdf_doc, annotation_type):
    response = create_annotation(client, pdf_doc, annotation_type)

    assert response.status_code == 201
    body = response.json()
    assert body['annotation_type'] == annotation_type
    assert body['page_number'] == 0
    assert body['user_id'] == user.id
    assert body['is_owner'] is True
    assert body['user_name'] == 'Alice Smith'

    sent = ANNOTATION_PAYLOADS[annotation_type]
    echoed = body['annotation_data']
    for key, value in sent.items():
        assert echoed[key] == value

    annotation = PDFAnnotation.objects.get(id=body['id'])
    assert annotation.annotation_type == annotation_type
    assert annotation.pdf_document_id == pdf_doc.id


@pytest.mark.django_db
def test_list_annotations(client, pdf_doc):
    for annotation_type in ANNOTATION_PAYLOADS:
        assert create_annotation(client, pdf_doc, annotation_type).status_code == 201

    response = client.get(f'/api/pdf-documents/{pdf_doc.id}/annotations')
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 5
    assert all(annot['is_owner'] is True for annot in body)
    assert {annot['annotation_type'] for annot in body} == {'pen', 'highlighter', 'text', 'stamp', 'cloud'}


@pytest.mark.django_db
def test_update_pen_annotation(client, pdf_doc):
    annotation_id = create_annotation(client, pdf_doc, 'pen').json()['id']

    new_vertices = [[[5.0, 5.0], [25.0, 40.0], [60.0, 15.0], [80.0, 80.0]]]
    new_erasures = [{'cx': 20.0, 'cy': 20.0, 'r': 5.0}]
    response = put_annotation(client, annotation_id, pen_data(vertices=new_vertices, erasures=new_erasures))
    assert response.status_code == 204

    annotation = PDFAnnotation.objects.select_related('pen_data').get(id=annotation_id)
    assert annotation.pen_data.vertices == new_vertices
    assert annotation.pen_data.erasures == new_erasures
    assert annotation.pen_data.border_width == 2.0


@pytest.mark.django_db
def test_delete_annotation(client, pdf_doc):
    annotation_id = create_annotation(client, pdf_doc, 'pen').json()['id']

    response = delete_annotation(client, annotation_id)
    assert response.status_code == 204
    assert not PDFAnnotation.objects.filter(id=annotation_id).exists()

    # A second delete hits the 'Annotation not found' path -> 404
    assert delete_annotation(client, annotation_id).status_code == 404


@pytest.mark.django_db
def test_other_user_cannot_get_pdf_detail(other_client, pdf_doc):
    response = other_client.get(f'/api/pdf-documents/{pdf_doc.id}')
    assert response.status_code == 404


@pytest.mark.django_db
def test_other_user_cannot_update_or_delete_annotation(client, other_client, pdf_doc):
    annotation_id = create_annotation(client, pdf_doc, 'pen').json()['id']

    response = put_annotation(other_client, annotation_id, pen_data())
    assert response.status_code == 403
    assert response.json()['detail'] == 'Access denied to PDF document'

    response = delete_annotation(other_client, annotation_id)
    assert response.status_code == 403
    assert response.json()['detail'] == 'Access denied to PDF document'
    assert PDFAnnotation.objects.filter(id=annotation_id).exists()


@pytest.mark.django_db
def test_other_user_list_annotations_returns_403(client, other_client, pdf_doc):
    create_annotation(client, pdf_doc, 'pen')
    response = other_client.get(f'/api/pdf-documents/{pdf_doc.id}/annotations')
    assert response.status_code == 403


@pytest.mark.django_db
def test_access_policy_override(client, other_client, pdf_doc):
    annotation_id = create_annotation(client, pdf_doc, 'pen').json()['id']

    with override_settings(SHERBERT_PDF_ACCESS_POLICY=ALLOW_ALL_POLICY_PATH):
        # Document access is now open to everyone...
        response = other_client.get(f'/api/pdf-documents/{pdf_doc.id}')
        assert response.status_code == 200
        assert response.json()['id'] == pdf_doc.id

        # ...but annotation ownership still applies.
        response = put_annotation(other_client, annotation_id, pen_data())
        assert response.status_code == 403
        assert response.json()['detail'] == 'You can only modify your own annotations'

    # Outside the override the default owner-only policy is back.
    assert other_client.get(f'/api/pdf-documents/{pdf_doc.id}').status_code == 404
