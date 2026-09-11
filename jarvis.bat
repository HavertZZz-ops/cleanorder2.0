@echo off
title Iniciar Projeto Django
echo =========================================
echo   Preparando o ambiente Django...
echo =========================================

:: 1. Verifica se a pasta venv existe, se não, cria uma
if not exist "venv\Scripts\activate" (
    echo [!] Ambiente virtual "venv" nao encontrado. Criando um novo...
    python -m venv venv
)

:: 2. Ativa o ambiente virtual
echo [*] Ativando o ambiente virtual...
call venv\Scripts\activate

:: 3. Instala as dependencias se o requirements.txt existir
if exist "requirements.txt" (
    echo [*] Verificando e instalando dependencias...
    pip install -r requirements.txt
) else (
    echo [!] Arquivo "requirements.txt" nao encontrado. Pulando instalacao.
)

:: 4. Verifica se o manage.py existe e inicia o servidor
if exist "manage.py" (
    echo [*] Iniciando o servidor Django...
    python manage.py runserver
) else (
    echo [!] "manage.py" nao encontrado. Verifique se o .bat esta na raiz do projeto.
)

pause