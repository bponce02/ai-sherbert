from django.urls import path

from .views import EditorView

app_name = 'sherbert_pdf'

urlpatterns = [
    path('editor/<int:pk>/', EditorView.as_view(), name='editor'),
]
