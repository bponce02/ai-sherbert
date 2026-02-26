from django.shortcuts import redirect
from django.views import View

from orange_sherbert.view import CRUDView

from .models import Author, Book, BookRequest, RequestComment


class OrderOnlineView(View):
    def post(self, request, pk):
        book = Book.objects.get(pk=pk)
        return redirect(f"https://www.barnesandnoble.com/s/{book.title}")


class CheckOutView(View):
    def post(self, request, pk):
        book = Book.objects.get(pk=pk)
        book.checked_out = True
        book.save()
        return redirect(request.META.get("HTTP_REFERER", "book-list"))


class CheckInView(View):
    def post(self, request, pk):
        book = Book.objects.get(pk=pk)
        book.checked_out = False
        book.save()
        return redirect(request.META.get("HTTP_REFERER", "book-list"))


class BookCRUDView(CRUDView):
    model = Book
    fields = {
        "title": "Title",
        "author": "Author",
        "isbn": "ISBN",
        "formatted_price": "Price",
        "pub_date": "Publication Date",
        "checked_out": "Checked Out",
    }
    form_fields = {
        "title": "Title",
        "author": "Author",
        "isbn": "ISBN",
        "price": "Price",
        "pub_date": "Publication Date",
        "checked_out": "Checked Out",
        "ordered_from": "Ordered From",
        "location": "Location",
    }
    filter_fields = {"author": "Author", "checked_out": "Checked Out"}
    search_fields = ["title", "isbn"]
    restricted_fields = {"ordered_from": "can_view_ordered_from"}
    property_field_map = {"formatted_price": "price"}
    field_widths = [20, 10, 10, 5, 5, 5]

    inline_formsets = [
        {
            "model": BookRequest,
            "fields": ["requester_name", "requester_email"],
            "can_delete": True,
        },
        {
            "model": RequestComment,
            "fields": ["comment"],
            "nested_under": BookRequest,
            "extra": 1,
            "can_delete": True,
        },
    ]
    extra_actions = [
        {
            "name": "order-online",
            "view": OrderOnlineView,
            "label": "Order Online",
            "method": "POST",
            # 'permission': 'can_order_online',
        },
        {
            "name": "check-in",
            "label": "Check In",
            "view": CheckInView,
            "method": "POST",
            #'permission': 'can_check_in'
        },
        {
            "name": "check-out",
            "label": "Check Out",
            "view": CheckOutView,
            "method": "POST",
            #'permission': 'can_check_out'
        },
    ]


class AuthorCRUDView(CRUDView):
    model = Author
    fields = "__all__"
    search_fields = ["name"]
