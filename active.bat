if exist "config\manage.py" (
    echo [*] Acessando a pasta config...
    cd config
    echo [*] Iniciando o servidor Django...
    python manage.py runserver
) else (
    echo [!] "manage.py" nao encontrado dentro da pasta "config".
)

pause
