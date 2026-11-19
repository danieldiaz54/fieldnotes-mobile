# PRODUCT.md — `fieldnotes-mobile`

## 1. Propósito en una frase
**App móvil de notas y listas sin internet**: captura notas e inspecciones desde tu teléfono en modo avión o zonas sin cobertura, y sincronízalas automáticamente al volver el Wi-Fi sin perder cambios.

## 2. Regla de 2 Capas (Usuario primero, Arquitectura bajo demanda)
- **Capa 1 (`Operate` Dispositivo Protagonista):** Un teléfono interactivo centrado sobre mesa de trabajo cálida. Incluye interruptor físico **`Modo Avión / Sin Wi-Fi`**, barra inferior de navegación al pulgar (`Notas`, `Pendientes`, `Conflictos`), creación rápida de notas en local (`POST /api/v1/notes/local`), botón **`Sincronizar ahora`** (`POST /api/v1/sync/run`), simulador **`Edición remota`** y tarjetas claras para resolver conflictos en 1 toque (`Conservar mía`, `Usar nube`, `Combinar`).
- **Capa 2 (Cajón `"Arquitectura"` opcional):** Muestra cola persistente SQLite Outbox (`/api/v1/sync/queue`), relojes lógicos de Lamport, registro inmutable (`/api/v1/sync/audit`) y detalles de Clean Architecture + React Native / Expo 52.
