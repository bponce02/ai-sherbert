"""
URL configuration for example project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path
from ninja import NinjaAPI

from sherbert_pdf.api import router as sherbert_pdf_router

from .views import BookCRUDView, AuthorCRUDView

api = NinjaAPI()
api.add_router('', sherbert_pdf_router)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', api.urls),
    *BookCRUDView.get_urls(),
    *AuthorCRUDView.get_urls(),
]
