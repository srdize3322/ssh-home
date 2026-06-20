# ssh-home

Gestor SSH interactivo y portable para equipos que ya tienen OpenSSH configurado.

`ssh-home` lee únicamente tu `~/.ssh/config` local, muestra los aliases disponibles, abre una conexión maestra temporal y te deja navegar carpetas remotas antes de entrar al shell definitivo. No usa inventarios privados, no depende de `.env` y no guarda contraseñas.

## Comando global

Si enlazas el launcher `ssh-home` dentro de un directorio que ya esté en tu `PATH` como `~/.local/bin`, puedes ejecutarlo desde cualquier carpeta:

```bash
ssh-home
```

Ejemplo de enlace:

```bash
ln -sfn /ruta/a/ssh-home/ssh-home ~/.local/bin/ssh-home
```

## Qué hace

- Lista aliases SSH detectados en `~/.ssh/config` y `Include` relacionados.
- Ignora entradas globales o wildcard como `Host *` y `Host jump-*`.
- Resuelve `HostName`, `User`, `Port` y `ProxyJump` con `ssh -G`.
- Reutiliza una conexión maestra temporal para evitar múltiples prompts de autenticación.
- Abre una TUI interactiva con flechas, filtro al escribir y navegación hacia atrás.
- Permite navegar directorios remotos antes de abrir la sesión final.
- Sale de la TUI antes de lanzar `ssh`, para que el shell remoto ocupe el terminal normal.
- Reutiliza la misma conexión autenticada del navegador remoto para abrir el shell final.
- Soporta ejecución interactiva incluso cuando el script llega por `curl | python3 -`.

## Requisitos

- `python3`
- Cliente `ssh` de OpenSSH
- Un `~/.ssh/config` válido en el equipo donde corras la herramienta

## Uso

```bash
./ssh-home
```

Alternativa equivalente:

```bash
python3 ssh-home.py
```

Flags disponibles:

```bash
./ssh-home --list
./ssh-home --list --show-resolved
./ssh-home --host app-prod
./ssh-home --host app-prod --path /srv/app/current
./ssh-home --config ~/.ssh/config
./ssh-home --host app-prod --show-resolved
./ssh-home --no-tui
```

Equivalentes con Python explícito:

```bash
python3 ssh-home.py --list
python3 ssh-home.py --list --show-resolved
python3 ssh-home.py --host app-prod
python3 ssh-home.py --host app-prod --path /srv/app/current
python3 ssh-home.py --config ~/.ssh/config
python3 ssh-home.py --host app-prod --show-resolved
python3 ssh-home.py --no-tui
```

## Flujo interactivo

1. Elige un alias SSH con flechas o escribiendo para filtrar.
2. Si el host pide contraseña, `ssh` la solicita con su prompt nativo.
3. Navega carpetas remotas:
   - `Up` / `Down`: mover selección
   - `Enter`: entrar o usar la carpeta actual
   - `Left` o `Backspace`: subir un nivel
   - escribir: filtrar directorios visibles
   - `/`: escribir una ruta manual
   - `Tab`: volver a la lista de hosts
   - `q`: cancelar
4. Se abre un shell remoto directamente dentro de la carpeta elegida.

## Ejecución vía curl

Reemplaza la URL por la raw URL real de tu repo pública cuando la publiques:

```bash
curl -fsSL https://raw.githubusercontent.com/tu-usuario/ssh-home/main/ssh-home.py | python3 -
```

También puedes descargarlo y ejecutarlo localmente:

```bash
curl -fsSL https://raw.githubusercontent.com/tu-usuario/ssh-home/main/ssh-home.py -o /tmp/ssh-home.py
python3 /tmp/ssh-home.py
```

## Seguridad

- No incluye claves, hosts reales, inventarios privados ni variables de entorno.
- Solo usa información que ya existe en la config SSH del equipo actual.
- La autenticación sigue ocurriendo en el binario `ssh` del sistema.
- El socket de control vive en `/tmp` y se limpia al salir.

## Desarrollo

Ejecutar tests:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

## Limitaciones

- El navegador remoto asume un shell POSIX con `sh`, `find`, `sed` y `sort`.
- Está pensado para macOS y Linux con OpenSSH disponible.
- No intenta editar ni sincronizar tu config SSH.
- Si el terminal no soporta `curses` o no tiene TTY real, cae a un modo texto simple como fallback.
