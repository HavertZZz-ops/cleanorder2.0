from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView


from ClearOrder.views import concluir_tarefa, fazer_logout, login_view, painel_estudante, painel_monitor, detalhes_quarto

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', RedirectView.as_view(pattern_name='login', permanent=False), name='raiz'),
    path('login/', login_view, name='login'),
    path('aluno/', painel_estudante, name='painel_estudante'),
    path('monitor/', painel_monitor, name='painel_monitor'),
    path('logout/', fazer_logout, name='logout'),
    path('tarefa/<int:tarefa_id>/concluir/', concluir_tarefa, name='concluir_tarefa'),
    path('quarto/<int:quarto_id>/', detalhes_quarto, name='detalhes_quarto'),
]