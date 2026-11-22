# Plan del proyecto: Agente de seguimiento de chats (WhatsApp con Evolution API, Gemini, web y Telegram, todo en Railway)

> Track: **Comercios**. Cada conversación de WhatsApp queda guardada, resumida, clasificada y con dueño, y nadie deja clientes colgados.
> Alternativas evaluadas y criterios de cambio: [ALTERNATIVAS.md](ALTERNATIVAS.md).

## 1. Qué se construye

Un servicio web para negocios y call centers que atienden clientes por WhatsApp.

1. Una persona entra a la web, crea su cuenta y pulsa **«Conectar WhatsApp»**. Vincula su número escaneando un QR o escribiendo un código de emparejamiento, igual que en «Dispositivos vinculados». Sigue usando WhatsApp en su teléfono sin cambios.
2. Cada mensaje de sus chats individuales llega por webhook desde Evolution API y se guarda en la base de datos. No hay exportación manual.
3. Cuando una conversación lleva 10 minutos sin mensajes nuevos, Gemini sugiere un **resumen**, un **estado**, la **siguiente acción** y una **fecha de seguimiento**.
4. La persona confirma o cambia el estado en la web.
5. Los recordatorios (seguimiento vencido, cliente esperando respuesta) aparecen en la web y llegan a su **Telegram**.

**Estados:** `vendida`, `cita_agendada`, `pendiente_seguimiento`, `resuelta`, `perdida`.

El sistema solo **escucha**. No envía mensajes por WhatsApp desde las sesiones de los clientes.

## 2. Decisiones

| Tema | Decisión |
|---|---|
| Conexión con WhatsApp | Evolution API v2.3.7 (versión fija), en el mismo contenedor que el backend. Una instancia por número conectado. |
| Registro del número | QR y código de emparejamiento. El número se envía al crear la instancia; sin él no hay código. Botón «reintentar» que no crea otra instancia. |
| Entrada de mensajes | Webhook `messages.upsert` de Evolution hacia el backend, por `127.0.0.1` dentro del contenedor. El token viaja en la cabecera `x-webhook-token`. |
| Envío de mensajes | Ninguno por WhatsApp. Los avisos al agente van por Telegram. |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2, `httpx`, `python-telegram-bot`, `google-genai`. |
| Base de datos | Neon (Postgres), proyecto `chat-followup` con dos bases: `app` (datos del producto) y `evolution` (sesiones de WhatsApp). Las pruebas usan SQLite. |
| Panel | Web servida por FastAPI (Jinja2 + HTMX). |
| LLM | `gemini-3.5-flash-lite` con salida JSON validada con Pydantic. |
| Quién decide el estado | La IA sugiere (`suggested_status`) y la persona confirma (`status`). |
| Despliegue | **Un solo servicio en Railway**, construido desde el `Dockerfile`. `start.sh` arranca Evolution y el backend en el mismo contenedor. |
| Segunda etapa | Adaptador para la Cloud API oficial de Meta con coexistencia (Tech Provider), como plan «oficial». |

## 3. Arquitectura

```
 Navegador ──HTTPS──► Railway: un solo servicio (Dockerfile)
                      ┌───────────────────────────────────────────────┐
                      │ backend FastAPI  (puerto $PORT, público)      │
                      │   /api/connections   crea la instancia, QR    │
                      │   /webhook/evolution recibe los eventos       │
                      │   web + tareas + bot de Telegram              │
                      │        ▲ 127.0.0.1:$PORT   │ 127.0.0.1:8081   │
                      │        │                   ▼                  │
                      │ Evolution API (solo dentro del contenedor)    │
                      └────────┬──────────────────┬───────────────────┘
                               ▼                  ▼
                  Neon · base `app`      Neon · base `evolution`
                  (cuentas, conversaciones,   (sesiones de WhatsApp)
                   mensajes)
```

- Railway enruta el dominio público solo al puerto `$PORT` (el backend). Evolution escucha en `127.0.0.1:8081` y no es accesible desde fuera.
- El backend habla con Evolution con la clave global `AUTHENTICATION_API_KEY`. Evolution envía los eventos al backend con el token en la cabecera `x-webhook-token`.
- `start.sh` arranca los dos procesos. Si uno termina, el contenedor se detiene y Railway lo reinicia.
- Evolution solo guarda las sesiones en su base. Los mensajes los guarda el backend en la base `app`.
- Se ejecuta una sola instancia del servicio.

### Flujo de un mensaje

```
Cliente ► WhatsApp ► sesión de Evolution ──POST messages.upsert──► /webhook/evolution
   guarda en messages (única por instancia + id del mensaje) ► actualiza la conversación
   ► tarea cada minuto: 10 min de silencio ► Gemini ► suggested_status ► web y Telegram
```

### Flujo de registro del número

```
Web ► POST /api/connections {nombre, número}
    ► backend: crea la instancia en Evolution, configura el webhook, pide el QR / código
    ► web muestra QR y código, y consulta el estado cada 2 s
    ► evento connection.update (state = open) ► la conexión pasa a «conectado»
    ► si se cae: connection.update (close) ► la web ofrece «reconectar»
```

### Modelo de datos

- `accounts(id, email, password_hash, name, telegram_id, link_token, tz, created_at)`.
- `connections(id, account_id, instance_name UNIQUE, phone, state, created_at, connected_at)`. `state`: `creating`, `qr`, `connecting`, `open`, `closed`.
- `conversations(id, connection_id, remote_jid, customer_name, summary, suggested_status, status, next_action, amount, follow_up_at, unanswered, done, ignored, last_message_at, last_analyzed_at, last_reminded_at)`. Única por `(connection_id, remote_jid)`.
- `messages(id, conversation_id, wa_message_id, direction, text, type, sent_at)`. Única por `(conversation_id, wa_message_id)`.
- `corrections(id, conversation_id, suggested_status, final_status, at)`.

Las fechas se guardan en UTC.

## 4. Fases

### Fase 0. Infra en Railway y prueba técnica de Evolution (1–2 días, con la persona responsable)
- [x] Proyecto de Neon `chat-followup` (región `aws-us-east-1`) con las bases `app` y `evolution`.
- [x] `Dockerfile` y `start.sh`: Evolution v2.3.7 y el backend en un solo contenedor, sin Redis. Probado en local contra Neon: Evolution aplica sus migraciones, exige su clave, crea instancias, genera el QR y el código de emparejamiento, y sus webhooks llegan al backend.
- [ ] Un solo servicio en Railway desde este repositorio, con dominio público y las variables de la sección 8. Si hay desconexiones o QR que no aparecen, añadir Redis.
- [ ] **Prueba técnica** con 5 números distintos, usando el código de emparejamiento y el QR:
  - Se registran a la primera o a la segunda.
  - Las 5 sesiones siguen conectadas 24–48 horas.
  - El webhook recibe los mensajes entrantes y los enviados desde el teléfono, sin duplicados.
  - Se anota qué trae `syncFullHistory` y el formato exacto del evento `messages.upsert`.
- [ ] Si Evolution no cumple, repetir la prueba con las alternativas de [ALTERNATIVAS.md](ALTERNATIVAS.md).

**Listo cuando:** la prueba se cumple y los resultados quedan anotados en `docs/prueba-evolution.md`.

### Fase 1. Núcleo de datos, cliente de Evolution y webhook (código)
- [x] Esquema en SQLAlchemy con las tablas de la sección 3 (`app/db.py`). Las tablas se crean al arrancar; Alembic se añade con el primer cambio de esquema en producción.
- [x] Cliente de Evolution (`httpx`): crear instancia, configurar webhook, pedir QR o código, consultar estado, cerrar sesión y borrar instancia (`app/channels/evolution.py`).
- [x] `POST /webhook/evolution` con verificación del token, extracción del texto de `messages.upsert`, filtrado de grupos y estados, y guardado idempotente (`app/channels/webhook.py`).
- [x] Conversación creada o actualizada por `(connection_id, remote_jid)`, con `last_message_at` y `unanswered` (`app/core/ingest.py`).
- [x] Manejo de `connection.update` y `qrcode.updated` para el estado de la conexión.
- [x] Pruebas con payloads de ejemplo de Evolution (`tests/`).
- [x] Esquema creado y guardado idempotente verificados contra Postgres (Neon).
- [x] Endpoints de Evolution verificados contra la versión 2.3.7. Quedan por contrastar los payloads reales de `messages.upsert` (lista en `docs/prueba-evolution.md`).

**Listo cuando:** un payload `messages.upsert` repetido deja una sola fila en `messages` y la conversación queda actualizada.

### Fase 2. Cuentas y registro del número
- [x] Registro e inicio de sesión: contraseña con scrypt, sesión por cookie firmada, correo sin distinguir mayúsculas (`app/web/auth.py`, `app/web/routes.py`).
- [x] Pantalla «Conectar WhatsApp»: número, QR y código de emparejamiento, estados (preparando, esperando escaneo, conectando, conectado, desconectado) y botón «Generar uno nuevo» que reutiliza la instancia (`app/core/connections.py`, `app/web/templates/`).
- [x] La página se actualiza sola: consulta el estado cada 3 s y renueva el QR cada 25 s. Si Evolution perdió la instancia, se vuelve a crear con el mismo nombre.
- [x] «Desconectar» borra la instancia y conserva los mensajes guardados. «Reconectar» desde el panel vuelve a mostrar el código.
- [x] Aviso en el formulario de que la conexión no es oficial, de que solo se leen los chats y de que conviene empezar con un número secundario.
- [x] Límite de 5 números por cuenta y aislamiento entre cuentas (otra cuenta recibe 404).
- [x] Probado de punta a punta contra Evolution real en Docker y en un navegador, sin errores de consola.
- [ ] Vincular un teléfono real con el código y ver llegar sus mensajes (requiere un número real).

**Listo cuando:** una persona registra su número desde la web y sus mensajes aparecen en la base de datos.

### Fase 3. Análisis con Gemini
- [x] Tarea cada minuto: conversaciones con 10 minutos de silencio y mensajes nuevos desde el último análisis, hasta 10 por ciclo (`app/core/analysis.py`).
- [x] `llm.analyze` sobre la conversación completa (recortando el inicio si es larga) y guardado en `suggested_status`, resumen, acción, monto y fecha. La persona confirma después (`status`).
- [x] Regla determinista: solo hay pregunta sin responder si el último mensaje es del cliente.
- [x] Modelo de respaldo (`GEMINI_FALLBACK_MODEL`) ante errores 503 y 429, y reintento a los 15 minutos si un análisis falla.
- [x] Evaluación con 30 conversaciones sintéticas (`samples/eval_chats.json`, `python -m app.eval`).
- [ ] Medir las 30 conversaciones completas. En la primera corrida, Gemini respondió 16 de 30 y acertó las 16; las otras 14 fallaron con 503 («alta demanda»).
- [x] Análisis manual: «Analizar ahora» en el detalle de cada chat y en el tablero (hasta 8 chats por pulsación, sin esperar el silencio).

**Listo cuando:** el estado coincide en al menos el 85 % de los casos y no hay JSON inválido.

### Fase 4. Web
- [x] Tablero kanban en modo claro (`/`): columnas Sin clasificar, Pendiente, Cita agendada, Vendida, Resuelta y Perdida. Cada tarjeta muestra el último mensaje como burbuja, el resumen de la IA, el tiempo de espera, el seguimiento y el monto (`app/core/board.py`, `app/web/templates/`, `app/web/static/`).
- [x] Franja de resumen por estado (con la suma de ventas) y una línea con lo que más urgente: «3 clientes esperan respuesta · 2 seguimientos vencidos».
- [x] Sugerencias por confirmar: la tarjeta muestra «IA sugiere …» con un botón «Confirmar»; la persona también puede arrastrarla a otra columna o elegir el estado en el detalle. Cada cambio se guarda en `corrections`.
- [x] Detalle de la conversación en un panel lateral: estado, resumen, siguiente acción, seguimiento, monto y los mensajes. Incluye «Analizar ahora».
- [x] Actualización automática cada 30 segundos y botón «Actualizar». Números conectados en `/numbers`.
- [x] «Ir al chat» en cada tarjeta y en el detalle: abre la conversación en WhatsApp con `https://wa.me/<número>` en una pestaña nueva. Los contactos sin número visible (`@lid`) no tienen enlace.
- [x] Diseño con el estilo «hoja enmarcada» del skill de diseño: marco verde-grisáceo, hoja blanca, sin modo oscuro. Inspector: 0 fallos y 0 avisos a 1440, 768 y 390.
- [x] Datos de demostración solo para desarrollo local: `python scripts/seed_demo.py` (cuenta `demo@demo.local`).
- [x] Chats personales: columna booleana `ignored` y estado «Personal». Al marcar un chat como personal se borran sus mensajes guardados y no se guarda ni se analiza nada más de ese contacto (se descarta en el webhook). «Volver a incluir» borra la marca y el próximo mensaje lo crea como chat nuevo. También se puede ignorar un número antes de que escriba. Pide confirmación porque borra mensajes.
- [x] Mover a mano: cada tarjeta tiene un botón «Mover» con una lista desplegable (Pendiente, Cita agendada, Vendida, Resuelta, Perdida y Personal). Se puede usar con teclado y desde el detalle. Se mantiene el arrastre entre columnas.
- [x] Clasificación refinada: orden de prioridad (perdida, vendida, cita, pendiente, resuelta). Una venta con entrega, recojo, instalación o primera clase con fecha sigue siendo «vendida», y esa fecha se muestra como «Entrega». Un presupuesto sin pago ni aceptación es «pendiente». Los 6 casos difíciles nuevos de `samples/eval_chats.json` salen 6/6.
- [x] Las bases existentes reciben la columna `ignored` al arrancar (`init_db` añade las columnas que faltan).
- [ ] Probar el tablero con conversaciones reales (tras desplegar y recibir mensajes).
- [ ] La lista «Hoy» queda cubierta por la línea de urgencias y las marcas de las tarjetas; una vista aparte se decide tras el piloto.

**Listo cuando:** una persona confirma 5 conversaciones y las ve en su tablero.

### Fase 5. Recordatorios y Telegram
- [ ] Vinculación de Telegram con `/start <link_token>` desde la cuenta.
- [ ] Tarea cada 15 minutos: seguimiento vencido o `unanswered = true`, con enfriamiento de 2 horas.
- [ ] Aviso por Telegram con botones «Hecho», «En 1 h» y enlace a la conversación.
- [ ] Horario silencioso y resumen diario a las 9 am.

**Listo cuando:** un seguimiento a 2 minutos dispara un solo aviso en la web y en Telegram.

### Fase 6. Piloto
- [ ] 2 o 3 personas con su número real, con consentimiento de sus clientes.
- [ ] Métricas: % de estados corregidos, tiempo ahorrado por chat, pendientes cerrados tras un recordatorio, sesiones caídas por semana.
- [ ] Ajustar el prompt con las correcciones recogidas.

### Fase 7. Conexión oficial y planes
- [ ] Verificación del negocio en Meta y App Review como Tech Provider.
- [ ] Adaptador de la Cloud API con Embedded Signup y coexistencia.
- [ ] Planes en la web: **Conexión rápida** (Evolution) y **Conexión oficial** (Cloud API).

### Fase 8. Después
- Varios usuarios por cuenta y panel de supervisor.
- Transcripción de audios con Gemini.
- Exportación a CRM.

## 5. Costos

| Concepto | Costo |
|---|---|
| Evolution API | Gratis. Se paga el servicio en Railway. Revisar su licencia antes de usarla comercialmente. |
| Railway (un servicio con Evolution y el backend) | Plan Hobby desde 5 USD al mes más el uso. El consumo real se mide en la Fase 0. |
| Neon (Postgres) | Plan gratuito para empezar. Evolution y las tareas periódicas mantienen la base despierta, así que hay que vigilar las horas de cómputo incluidas y pasar a un plan de pago si se agotan. |
| Gemini Flash-Lite | Unos 0,002 USD por análisis. Una cuenta con 100 conversaciones al día: unos 6 USD al mes. En producción se usa el nivel de pago, porque en el gratuito Google puede usar los datos. |
| Telegram Bot API | Gratis. |
| WhatsApp Cloud API oficial (Fase 7) | Recibir no se cobra. Se cobran los mensajes de plantilla enviados. Tarifas de terceros para «Resto de Latinoamérica» (incluye Bolivia): marketing 0,0740 USD, utilidad y servicio 0,0113 USD. Confirmar en Meta. |

**Precio sugerido para el plan «Conexión rápida»:** el costo por número (Gemini más la parte de infraestructura) ronda los 6–10 USD al mes. Una hipótesis de partida es cobrar entre 15 y 20 USD al mes por número, y validarla en el piloto.

## 6. Riesgos

| Riesgo | Mitigación |
|---|---|
| WhatsApp bloquea un número vinculado por una conexión no oficial | Solo se escucha, sin enviar. Aviso al registrarse. Plan «Conexión oficial» en la Fase 7. |
| El registro del número falla o se pierde la sesión | Versión fija de Evolution, QR y código de emparejamiento, reconexión automática y la prueba de la Fase 0. |
| Un cambio de protocolo de WhatsApp rompe Evolution | Actualizar la versión solo tras repetir la prueba. Alternativas en [ALTERNATIVAS.md](ALTERNATIVAS.md). |
| Licencia de Evolution incompatible con el uso comercial | Leer la licencia en la Fase 0. Si no sirve, cambiar de proveedor con el mismo adaptador. |
| Datos personales de clientes | Consentimiento, acceso por cuenta, Gemini de pago y anonimización en las pruebas. |
| Clasificación errónea | La IA solo sugiere. La persona confirma y se mide el acierto. |
| Entregas duplicadas del webhook | Clave única por instancia e id del mensaje. |
| Evolution expuesto a internet | Escucha solo en `127.0.0.1` dentro del contenedor. Railway solo enruta el puerto del backend. |
| Evolution y el backend comparten contenedor | Si uno cae, el contenedor se reinicia con los dos. Un fallo de Evolution corta el webhook hasta que vuelve. |
| La cuota de cómputo gratuita de Neon se agota | Medirla en el piloto. Alternativa: plan de pago de Neon. |

## 7. Estructura de carpetas

```
Track-Comun/
├── PLAN.md
├── ALTERNATIVAS.md
├── README.md
├── requirements.txt
├── Dockerfile          Evolution + backend en una sola imagen
├── start.sh            arranca los dos procesos
├── railway.toml
├── .env.example
├── docs/
│   └── prueba-evolution.md
├── app/
│   ├── main.py             FastAPI y arranque de tareas
│   ├── config.py
│   ├── db.py               modelos y sesión de SQLAlchemy
│   ├── core/
│   │   └── ingest.py       guardar mensajes y actualizar conversaciones
│   ├── channels/
│   │   ├── evolution.py    cliente HTTP y lectura de payloads
│   │   ├── webhook.py      rutas del webhook
│   │   └── telegram.py     avisos y botones
│   ├── llm.py
│   ├── reminders.py
│   └── web/                rutas y plantillas
└── tests/
```

## 8. Variables de entorno

Obligatorias en Railway: `DATABASE_URL` (Neon, base `app`), `EVOLUTION_DATABASE_URL` (Neon, base `evolution`), `EVOLUTION_API_KEY`, `WEBHOOK_TOKEN`, `PUBLIC_URL`, `SESSION_SECRET`, `TZ`.

Para las fases siguientes: `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `SILENCE_MINUTES`, `REMINDER_INTERVAL_MIN`, `REMINDER_COOLDOWN_MIN`.

`start.sh` deriva el resto: `DATABASE_CONNECTION_URI` y `AUTHENTICATION_API_KEY` de Evolution, `EVOLUTION_URL` y `WEBHOOK_BASE_URL` (ambos hacia `127.0.0.1`).
