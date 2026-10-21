# -*- coding: utf-8 -*-
"""
Gatilhos automaticos de notificacao.

Reutiliza a model Notificacao que ja existe no projeto (campo
`estudante_destinatario` + `data_envio`), sem criar nem alterar campos.

Dois gatilhos:

1) Vistoria salva -> notifica todos os estudantes daquele quarto.
   Só dispara na CRIAÇÃO (created=True). Um `save()` posterior nao duplica.

2) SolicitacaoReparo muda de status -> notifica o autor do chamado,
   mas apenas na TRANSIÇÃO para "Verificado". Para isso comparamos o status
   antigo (pre_save) com o novo (post_save); sem isso, salvar o mesmo
   chamado varias vezes geraria notificações repetidas.
"""

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import Estudante, Notificacao, SolicitacaoReparo, Vistoria

STATUS_VERIFICADO = 'Verificado'


@receiver(pre_save, sender=SolicitacaoReparo)
def guardar_status_anterior(sender, instance, **kwargs):
    """Guarda o status antes do UPDATE, para deteccao de mudanca real."""
    if not instance.pk:
        instance._status_anterior = None
        return
    instance._status_anterior = (
        sender.objects.filter(pk=instance.pk).values_list('status', flat=True).first()
    )


@receiver(post_save, sender=Vistoria)
def notificar_vistoria(sender, instance, created, **kwargs):
    """Vistoria concluida -> um aviso por estudante do quarto."""
    if not created:
        # apenas salvar de novo nao e um novo evento
        return

    situacao = 'aprovada' if instance.aprovado else 'reprovada'
    titulo = 'Vistoria %s!' % situacao.capitalize()

    quando = instance.data_vistoria.strftime('%d/%m/%Y')
    if instance.nota is None:
        mensagem = 'Sua vistoria de %s foi %s.' % (quando, situacao)
    else:
        mensagem = 'Sua vistoria de %s foi %s. Nota: %d/10.' % (
            quando, situacao, instance.nota
        )

    for estudante in Estudante.objects.filter(quarto=instance.quarto):
        Notificacao.objects.create(
            estudante_destinatario=estudante,
            titulo=titulo,
            mensagem=mensagem,
        )


@receiver(post_save, sender=SolicitacaoReparo)
def notificar_chamado_verificado(sender, instance, created, **kwargs):
    """Chamado passa a "Verificado" -> avisa o estudante que abriu."""
    if created:
        # na criacao o status ainda e "Em Aberto"
        return
    if getattr(instance, '_status_anterior', None) == instance.status:
        # status nao mudou: nada a avisar
        return
    if instance.status != STATUS_VERIFICADO:
        return

    Notificacao.objects.create(
        estudante_destinatario=instance.estudante,
        titulo='Chamado verificado',
        mensagem='Seu chamado "%s" foi verificado pelo monitor.' % instance.titulo,
    )
