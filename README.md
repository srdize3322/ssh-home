# ssh-home

Gestor SSH interactivo y portable para equipos que ya tienen OpenSSH configurado. Proyecto de `srdize3322`.

`ssh-home` lee únicamente tu `~/.ssh/config` local, muestra los aliases disponibles, abre una conexión maestra temporal y te deja navegar carpetas remotas antes de entrar al shell definitivo. No usa inventarios privados, no depende de `.env` y no guarda contraseñas.

La interfaz tiene un look sobrio de homelab/cockpit: header `ssh-home :: project by srdize3322`, badge `ssh://home`, colores discretos si el terminal los soporta y un mini gráfico local de favoritos/recientes/otros hosts.

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
- Abre una TUI estilo homelab/cockpit con favoritos, recientes, filtro al escribir y navegación hacia atrás.
- Muestra un cockpit sobrio con logo `ssh://home`, metadata del host y mini gráfico de hosts.
- Permite navegar directorios remotos antes de abrir la sesión final.
- Sale de la TUI antes de lanzar `ssh`, para que el shell remoto ocupe el terminal normal.
- Reutiliza la misma conexión autenticada del navegador remoto para abrir el shell final.
- Guarda favoritos, recientes y última ruta por host en estado local privado.
- Soporta ejecución interactiva incluso cuando el script llega por `curl | python3 -`.

## Requisitos

- `python3`
- Cliente `ssh` de OpenSSH
- Un `~/.ssh/config` válido en el equipo donde corras la herramienta

## Uso

```bash
ssh-home
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
./ssh-home --state-file /tmp/ssh-home-state.json
./ssh-home --no-state
./ssh-home --clear-history
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
python3 ssh-home.py --state-file /tmp/ssh-home-state.json
python3 ssh-home.py --no-state
python3 ssh-home.py --clear-history
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
   - `f`: marcar o desmarcar favorito
   - `l`: saltar a la última ruta usada en el host
   - `r`: ver recientes en hosts o refrescar directorio remoto
   - `a`: volver a todos los hosts
   - `?`: mostrar ayuda compacta
   - `Tab`: volver a la lista de hosts
   - `q`: cancelar
4. Se abre un shell remoto directamente dentro de la carpeta elegida.

## Interfaz visual

- En terminales amplias muestra lista de hosts, panel lateral con metadata y gráfico compacto.
- En terminales medianas usa un panel compacto sin elementos que choquen.
- En terminales pequeñas oculta logo/gráfico y deja una vista mínima usable.
- Si `curses` no puede usar color, mantiene la misma navegación en monocromo.

## Estado local

Por defecto `ssh-home` guarda estado local en:

```bash
~/.config/ssh-home/state.json
```

Ese archivo puede contener aliases SSH, favoritos, recientes y últimas rutas remotas. No guarda contraseñas, llaves, IPs resueltas ni variables de entorno.

Para usar otro archivo:

```bash
ssh-home --state-file /tmp/ssh-home-state.json
```

Para desactivar el estado:

```bash
ssh-home --no-state
```

Para limpiar recientes y últimas rutas sin borrar favoritos:

```bash
ssh-home --clear-history
```

## Ejecución vía curl

```bash
curl -fsSL https://raw.githubusercontent.com/srdize3322/ssh-home/main/ssh-home.py | python3 -
```

También puedes descargarlo y ejecutarlo localmente:

```bash
curl -fsSL https://raw.githubusercontent.com/srdize3322/ssh-home/main/ssh-home.py -o /tmp/ssh-home.py
python3 /tmp/ssh-home.py
```

## Seguridad

- No incluye claves, hosts reales, inventarios privados ni variables de entorno.
- Solo usa información que ya existe en la config SSH del equipo actual.
- La autenticación sigue ocurriendo en el binario `ssh` del sistema.
- El socket de control vive en `/tmp` y se limpia al salir.
- El estado local es privado del equipo y nunca forma parte de la repo.

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
