# Resumen — Handoff IA ↔ Humano en Francisca (República Ciclismo)

> Documento de contexto. Última actualización: **2026-07-17**.
> Sirve para retomar el trabajo sin perder el hilo de lo que se construyó y del estado del debugging.

---

## 1. Qué se construyó

Se agregó **control de conversación por modos (`ia` / `humano`)** al agente de WhatsApp "Francisca", de forma **incremental** sobre el agente que ya existía (no se reescribió nada). Objetivo:

1. Un humano (Camila) puede responder como el negocio desde un panel web.
2. Cuando el humano interviene, Francisca se **silencia** automáticamente en esa conversación.
3. La conversación puede **volver a Francisca** manualmente o por inactividad (auto-retorno).
4. Francisca puede **escalar a un humano** como acción real (cambia el modo), no solo como frase.

---

## 2. Repositorio, ramas y deploy

- **Repo:** `fquinzio/whatsapp-agentkit`
- **Rama de producción (Railway auto-deploya de aquí):** `claude/build-agent-vck6l6`
- **Rama de desarrollo del feature:** `claude/francisca-ia-humano-modes-06dyut` (mismo contenido; ya mergeada a la de producción)
- **Deploy:** Railway, servicio `whatsapp-agentkit`. Auto-deploy en cada push a `claude/build-agent-vck6l6`.
- **URL del backend:** `https://whatsapp-agentkit-production-246b.up.railway.app`
- **Commit final del feature:** `899f45b` (confirmado desplegado en Railway).

### Cómo verificar que el código nuevo está corriendo
Abrir en el navegador:
```
https://whatsapp-agentkit-production-246b.up.railway.app/admin/conversaciones/probe/modo
```
- `{"detail":"Method Not Allowed"}` (405) → código nuevo desplegado ✅
- `{"detail":"Not Found"}` (404) → todavía el código viejo

(Confirmado hoy: devuelve **405** → el handoff está desplegado.)

---

## 3. Panel de conversaciones (Netlify)

- **Archivo:** `panel-conversaciones.html` (raíz del repo).
- **URL en vivo:** `https://stellular-choux-28288d.netlify.app`
- **Standalone:** guarda la URL del backend + el `ADMIN_TOKEN` en `localStorage`. Auto-refresh cada 30 s.
- **Funciones:** lista de conversaciones con badge de modo (🤖 IA / 🙋 Humano + tiempo en ese modo), botones **Tomar control** / **Devolver a Francisca**, caja para **Responder** como humano, y distinción visual entre mensajes de Cliente / Francisca / Humano.
- El backend **no** sirve el panel: es un archivo aparte hosteado en Netlify.

---

## 4. Cambios en el código (8 commits)

| Commit | Qué hace |
|---|---|
| `memory.py` | Tabla `conversation_state` + `get_modo` / `get_estado` / `set_modo` (default seguro `ia`). Helpers de auto-retorno. `obtener_historial` remueve el prefijo interno `[HUMANO]` antes de pasar a Claude. |
| `main.py` (webhook) | **Check 1**: si modo=humano, guarda el mensaje y avisa a Camila, pero Francisca no llama al LLM ni responde. **Check 2 (anti-carrera)**: re-consulta el modo justo antes de enviar; descarta la respuesta si un humano intervino durante la generación (salvo que el cambio lo haya hecho la propia Francisca al escalar). Ambos checks fallan "abierto" hacia `ia`. |
| `main.py` (endpoints) | `POST /admin/conversaciones/{telefono}/responder` y `POST /admin/conversaciones/{telefono}/modo`; `modo` y `modo_desde` agregados al `GET /admin/conversaciones`. |
| `tools.py` + `brain.py` + `prompts.yaml` | Tool `escalar_a_humano` (set_modo humano + notifica a Camila + despedida). Prompt ajustado para usar la tool. |
| `main.py` (auto-retorno) | Tarea asyncio en el startup (loop cada 10 min) que devuelve a IA las conversaciones humanas inactivas > `AUTO_RETORNO_MINUTOS`. Tolerante a fallos. |
| `panel-conversaciones.html` | Panel con los controles de modo. |
| `brain.py` (timezone) | `obtener_fecha_actual_texto()` usa `ZoneInfo("America/Santiago")` (antes usaba la hora UTC del servidor). |
| `memory.py` + `brain.py` (métricas) | Tabla `ia_metrics` (modelo, latencia, escalado, tokens) registrada por turno, sin romper la respuesta si falla. |

### Endpoints admin (todos requieren header `x-admin-token`)
- `GET  /admin/conversaciones` → lista (incluye `modo`, `modo_desde`)
- `GET  /admin/conversaciones/{telefono}` → historial completo
- `POST /admin/conversaciones/{telefono}/responder` body `{"mensaje": "..."}` → envía como humano y toma control; si Meta falla devuelve **502** (no 200 silencioso)
- `POST /admin/conversaciones/{telefono}/modo` body `{"modo":"ia"|"humano","nota":"..."}` → cambia el modo

### Tablas nuevas en la BD (Postgres, se crean solas con `create_all` al arrancar)
- `conversation_state` → `telefono` (PK), `modo`, `modo_desde`, `cambiado_por`, `nota`
- `ia_metrics` → `id`, `telefono`, `modelo`, `latencia_ms`, `escalado`, `input_tokens`, `output_tokens`, `timestamp`

---

## 5. Variables de entorno (Railway)

| Variable | Valor | Estado |
|---|---|---|
| `AUTO_RETORNO_MINUTOS` | `60` (0 = desactivado) | agregar/confirmar |
| `ADMIN_TOKEN` | secreto del panel (rotar; el anterior circuló en texto plano) | rotar y poner en el panel |
| `ANTHROPIC_API_KEY` | — | ya existía |
| `META_ACCESS_TOKEN`, `META_PHONE_NUMBER_ID`, `META_VERIFY_TOKEN` | — | ya existían |
| `CAMILA_WHATSAPP_NUMBER` | número de Camila | ya existía |
| `DATABASE_URL` | Postgres de Railway | ya existía |

- `META_VERIFY_TOKEN`: confirmado que el valor efectivo es **`agentkit-verify`** (el autotest del webhook devolvió `12345` con ese token).

---

## 6. Estado del debugging (lo importante)

### ✅ Confirmado funcionando
- App viva en Railway; código nuevo desplegado (ruta `/modo` responde 405).
- Base de datos inicializada, tablas nuevas creadas, auto-retorno activo (log: `Auto-retorno activo: 60 min`).
- Panel Netlify ↔ backend conectados (se ven `GET /admin/conversaciones 200 OK` y el preflight `OPTIONS`).
- **El flujo de respuesta de Francisca funciona de punta a punta.** Con el payload de prueba de Meta se vio en los logs:
  `POST /webhook 200 OK` → `Mensaje de 16315551181` → `Respuesta generada` → `POST graph.facebook.com 200 OK` → `Respuesta a 16315551181: …`
- Verificación del webhook (GET) correcta con token `agentkit-verify`.

### ❗ Problema abierto: no llegan los mensajes de teléfonos REALES
- El único mensaje que llegó al webhook fue el **payload de prueba** de Meta (`from: 16315551181`, texto `"this is a text message"`), que se dispara con el botón **"Test"** de la suscripción del webhook. **No es un mensaje real.**
- El error `131026 Message undeliverable` que se vio es **esperado**: ese número de prueba no puede recibir respuestas. No es un bug.
- Al mandar desde un teléfono real: "no pasa nada" y no aparece la conversación en el panel.
- Hoy se pasó la app de **Development → Live / Publicada** (se cargó URL de política de privacidad + categoría "Mensajes"). Tras esto, **según el usuario sigue sin funcionar.**

### 🔑 Conclusión sobre el código
El código **no es la causa** de que no lleguen los mensajes reales. Razón técnica: en el webhook, la línea de log `Mensaje de <número>` se escribe **antes** de cualquier lógica nueva del handoff. Por lo tanto:

> **Si al mandar desde un teléfono real NO aparece `Mensaje de 569…` en los logs de Railway, el mensaje no está llegando a la app → es entrega de Meta, no el código.**

Solo si aparece `Mensaje de 569…` y ahí Francisca no responde, recién habría que sospechar de la lógica (modo humano, etc.).

---

## 7. Próximos pasos para destrabar la recepción (lado Meta)

Con los **logs de Railway abiertos**, mandar un WhatsApp desde un teléfono real y ver si aparece `POST /webhook` + `Mensaje de 569…`.

Si **no aparece nada**, revisar en `developers.facebook.com` → app "Agente Francisca":

1. **WABA suscrita a la app**: WhatsApp → Configuration → que la suscripción al campo **`messages`** esté activa. Volver a suscribir si hace falta (a veces se cae tras un cambio de modo o redeploy).
2. **Número correcto**: en API Setup, confirmar que el `META_PHONE_NUMBER_ID` de Railway corresponde al **número real** al que se le está escribiendo (no al número de prueba de Meta).
3. **Estado del número**: WhatsApp → Phone Numbers → que el número esté **"Connected"**, con nombre para mostrar aprobado.
4. **Método de pago**: la WABA en Live suele requerir un método de pago configurado para poder enviar/recibir con normalidad.
5. **Callback URL** (confirmada correcta): `https://whatsapp-agentkit-production-246b.up.railway.app/webhook`, verify token `agentkit-verify`.
6. Tras cualquier cambio, reintentar el envío desde el teléfono real y mirar el log.

Si **sí aparece** `Mensaje de 569…` pero Francisca no responde:
- ¿El log dice `Modo humano activo para 569…`? → esa conversación está en modo humano; devolver a IA desde el panel o:
  ```
  curl -s -X POST -H "x-admin-token: TU_ADMIN_TOKEN" -H "Content-Type: application/json" \
    -d '{"modo":"ia"}' \
    https://whatsapp-agentkit-production-246b.up.railway.app/admin/conversaciones/TU_NUMERO/modo
  ```
- ¿Hay un error / traceback tras `Mensaje de …`? → copiar el traceback para diagnosticar.

---

## 8. Pruebas de aceptación del feature (cuando ya lleguen los mensajes)

1. Cliente escribe → Francisca responde normal (regresión).
2. Panel → **Responder** → llega al cliente, la conversación pasa a 🙋 Humano, y el siguiente mensaje del cliente **no** recibe respuesta de la IA.
3. **Devolver a Francisca** → el siguiente mensaje vuelve a ser respondido por la IA.
4. Cliente escribe "quiero hablar con una persona" → Francisca **escala** (modo humano, avisa a Camila con motivo, manda solo la despedida).
5. "¿Qué día es mañana?" → fecha correcta en hora de Chile.
6. `AUTO_RETORNO_MINUTOS=2` temporal → tomar control, esperar → vuelve sola a IA. Luego volver a `60`.

---

## 9. Fuera de alcance (para después)
- Migración del inbox a **Chatwoot** (reemplazaría el panel casero).
- **Plantilla utility aprobada en Meta** para notificar a Camila sin depender de la ventana de 24 h.
- RAG / conexión al catálogo Shopify en vivo.
