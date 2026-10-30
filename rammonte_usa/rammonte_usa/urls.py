"""
URL configuration for rammonte_usa project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
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
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include

from core import views as core_views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('i18n/', include('django.conf.urls.i18n')),
    # Health probe used by the hosting platform; kept above everything so it
    # never depends on an app being reachable.
    path('healthz/', core_views.health, name='health'),
    path('manifest.webmanifest', core_views.manifest, name='manifest'),
    path('sw.js', core_views.service_worker, name='service_worker'),
    path('accounts/', include('accounts.urls')),
    path('manage/', include('dashboard.urls')),
    path('', include('videos.urls')),
    path('shorts/', include('shorts.urls')),
]
# Serve the locally generated demo media while recording the walkthrough
# videos. In production the media is served by Cloudflare R2, not Django.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
