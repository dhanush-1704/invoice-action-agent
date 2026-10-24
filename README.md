# Invoice Action Agent

A2A 1.0 invoice-claim batch agent deployed as Vercel Python Functions.

## Required environment variables

- `A2A_BEARER_TOKEN`: exact Bearer credential accepted by the API.
- `BLOB_READ_WRITE_TOKEN`: Vercel Blob read/write token for durable state. Do not commit either value.

## Endpoints

- `GET /.well-known/agent-card.json` — public agent card
- `POST /message:send` — A2A message submission
- `GET /tasks` — list tasks belonging to the authenticated principal
- `GET /tasks/{id}` — read an owned task
- `POST /tasks/{id}:cancel` — cancel an owned task

Protected endpoints require `Authorization: Bearer …`, `A2A-Version: 1.0`, and `Content-Type: application/a2a+json` where a request body is sent.

## Deployment

Connect this repository to the existing Vercel project `invoice-action-agent`, configure the required environment variables in Vercel, and deploy. Never run the grader until the deployed endpoints and persistence have been verified.