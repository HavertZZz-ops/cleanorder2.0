from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView


from ClearOrder.views import concluir_tarefa, enviar_relatorio, fazer_logout, login_view, painel_estudante, painel_monitor, detalhes_quarto, progresso, reportar_problema, verificar_reparo, chamados_view, gerar_cronograma_view, simular_proximo_dia, marcar_notificacao_lida, limpar_notificacoes, gestao_monitor_view, atualizar_foto_perfil, vistoria_quarto, analytics_json, qrcode_quarto_svg, exportar_relatorio_csv, relatorio_pdf_view;

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', RedirectView.as_view(pattern_name='login', permanent=False), name='raiz'),
    path('login/', login_view, name='login'),
    path('aluno/', painel_estudante, name='painel_estudante'),
    path('monitor/', painel_monitor, name='painel_monitor'),
    path('monitor/simular-data/', simular_proximo_dia, name='simular_proximo_dia'),
    path('monitor/gestao/', gestao_monitor_view, name='gestao_monitor'),
    path('logout/', fazer_logout, name='logout'),
    path('tarefa/<int:tarefa_id>/concluir/', concluir_tarefa, name='concluir_tarefa'),
    path('quarto/<int:quarto_id>/', detalhes_quarto, name='detalhes_quarto'),
    path('chamados/', chamados_view, name='chamados'),
    path('reparo/<int:reparo_id>/verificar/', verificar_reparo, name='verificar_reparo'),
    path('cronograma/gerar/', gerar_cronograma_view, name='gerar_cronograma'),
    path('notificacoes/marcar-lida/<int:notificacao_id>/', marcar_notificacao_lida, name='marcar_notificacao_lida'),
    path('notificacoes/limpar/', limpar_notificacoes, name='limpar_notificacoes'),
    path('reportar/', reportar_problema, name='reportar_problema'),
    path('progresso/', progresso, name='progresso'),
    path('estudante/enviar-relatorio/', enviar_relatorio, name='enviar_relatorio'),# Adicione esta linha para a URL de progresso
    path('perfil/foto/', atualizar_foto_perfil, name='atualizar_foto_perfil'),
    # QR Code da porta do quarto aponta para cá.
    path('monitor/vistoria/<int:quarto_id>/', vistoria_quarto, name='vistoria_quarto'),
    path('monitor/analytics/', analytics_json, name='analytics_json'),
    path('monitor/quarto/<int:quarto_id>/qrcode-svg/', qrcode_quarto_svg, name='qrcode_quarto_svg'),
    # Exportacao do relatorio oficial (rota nova; nada existente foi mexido).
    path('gestao/exportar/csv/', exportar_relatorio_csv, name='exportar_relatorio_csv'),
    path('gestao/exportar/pdf/', relatorio_pdf_view, name='relatorio_pdf'),
]

# Fotos de chamado e de perfil so sao servidas em DEBUG. Em producao quem
# serve /media/ e o servidor web (nginx/apache), nunca o Django.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
