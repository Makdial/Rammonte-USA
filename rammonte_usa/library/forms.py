from django import forms

from .models import Book


class BookForm(forms.ModelForm):
    file = forms.FileField(required=True)
    cover = forms.FileField(required=False)

    class Meta:
        model = Book
        fields = ["title", "author", "description", "is_public"]