from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout 
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.utils import timezone

from .models import Estudante, Monitor, Tarefa, Quarto

def login_view(request):
    if request.user.is_authenticated:
        if hasattr(request.user, 'estudante'):
            return redirect('painel_estudante')
        elif hasattr(request.user, 'monitor'):
            return redirect('painel_monitor')
        else:
            return redirect('/admin/')

    if request.method == 'POST':
        cpf_digitado = request.POST.get('cpf') 
        senha_digitada = request.POST.get('password')
        
        username_para_login = cpf_digitado 
        
        try:
            estudante_encontrado = Estudante.objects.get(cpf=cpf_digitado)
            username_para_login = estudante_encontrado.user.username
        except Estudante.DoesNotExist:
            try:
                monitor_encontrado = Monitor.objects.get(cpf=cpf_digitado)
                username_para_login = monitor_encontrado.user.username
            except Monitor.DoesNotExist:
                pass

        usuario = authenticate(request, username=username_para_login, password=senha_digitada)

        if usuario is not None:
            login(request, usuario)

            if hasattr(usuario, 'estudante'):
                return redirect('painel_estudante')
            elif hasattr(usuario, 'monitor'):
                return redirect('painel_monitor')
            else:
                return redirect('/admin/')
                
        else:
            messages.error(request, 'CPF ou senha inválidos. Tente novamente.')

    return render(request, 'login.html')


def fazer_logout(request):
    logout(request)
    messages.success(request, 'Logout realizado com sucesso.')
    return redirect('login')


@login_required
def painel_estudante(request):
    if hasattr(request.user, 'estudante'):
        estudante = request.user.estudante
        tarefas = Tarefa.objects.filter(estudante_responsavel=estudante)
        print(f"Tarefas encontradas para o estudante {estudante}: {tarefas}")
        contexto = {
            'estudante': estudante,
            'tarefas': tarefas
        }
        # CORREÇÃO AQUI: Passando 'contexto' para o template enxergar as variáveis
        return render(request, 'painel_estudante.html', contexto)
    elif hasattr(request.user, 'monitor'):
        return redirect('painel_monitor')
    else:
        logout(request) 
        messages.error(request, 'Acesso negado: Sua conta não possui um perfil válido. Procure a administração.')
        return redirect('login') 


@login_required
def concluir_tarefa(request, tarefa_id):
    if not hasattr(request.user, 'estudante'):
        messages.error(request, 'Apenas estudantes podem concluir tarefas.')
        return redirect('login')

    tarefa = get_object_or_404(Tarefa, id=tarefa_id, estudante_responsavel=request.user.estudante)
    
    if tarefa.status == 'Concluída':
        tarefa.status = 'Pendente'
    else:
        tarefa.status = 'Concluída'
    tarefa.save()

    messages.success(request, 'Tarefa marcada como concluída com sucesso!')
    return redirect('painel_estudante')

@login_required
def painel_monitor(request):
    if hasattr(request.user, 'monitor'):
        estudantes = Estudante.objects.all()
        
        total_pendentes = estudantes.filter(status='Pendente').count()

        contexto = {
            'estudantes': estudantes,
            'total_pendentes': total_pendentes,
            'total_quartos': estudantes.count(),
        }
        return render(request, 'painel_monitor.html', contexto)
    elif hasattr(request.user, 'estudante'):
        return redirect('painel_estudante')
    else:
        logout(request)
        messages.error(request, 'Acesso negado.')
        return redirect('login')
    
@login_required
def detalhes_quarto(request, quarto_id):
    if hasattr(request.user, 'monitor'):
        

        quarto_selecionado = get_object_or_404(Quarto, id=quarto_id)

        contexto = {
            'quarto': quarto_selecionado
        }
        
        
        return render(request, 'formulario_quarto.html', contexto)
        
  
    elif hasattr(request.user, 'estudante'):
        return redirect('painel_estudante')
    else:
        return redirect('login')