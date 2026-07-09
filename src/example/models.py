from django.db import models

class Book(models.Model):
    title = models.CharField(max_length=100,verbose_name='Title')
    author = models.ForeignKey('Author', on_delete=models.CASCADE,verbose_name='Author')
    isbn = models.CharField(max_length=13,verbose_name='ISBN')
    price = models.DecimalField(max_digits=10, decimal_places=2,verbose_name='Price')
    pub_date = models.DateField(verbose_name='Publication Date')
    checked_out = models.BooleanField(default=False,verbose_name='Checked Out')
    ordered_from = models.CharField(max_length=100, blank=True, null=True, verbose_name='Ordered From')
    location = models.CharField(max_length=100, blank=True, null=True, verbose_name='Location')
    
    @property
    def formatted_price(self):
        return f"${self.price:,}"

    class Meta:
        verbose_name = 'Book'
        verbose_name_plural = 'Books'
        permissions = [
            ('can_view_ordered_from', 'Can view ordered from field'),
            ('can_check_out', 'Can check out a book'),
        ]
    
    def __str__(self):
        return self.title

class Author(models.Model):
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name

class BookRequest(models.Model):
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name='requests')
    requester_name = models.CharField(max_length=100, verbose_name='Name')
    requester_email = models.EmailField(verbose_name='Email')
    request_date = models.DateField(auto_now_add=True, verbose_name='Request Date')
    
    class Meta:
        verbose_name = 'Book Request'
        verbose_name_plural = 'Book Requests'
        ordering = ['request_date']
    
    def __str__(self):
        return f"{self.requester_name} - {self.book.title}"

class RequestComment(models.Model):
    request = models.ForeignKey(BookRequest, on_delete=models.CASCADE, related_name='comments')
    comment = models.TextField(verbose_name='Comment')
    
    class Meta:
        verbose_name = 'Request Comment'
        verbose_name_plural = 'Request Comments'
        ordering = ['id']
    
    def __str__(self):
        return f"{self.comment[:50]} - {self.request.requester_name}"
    
