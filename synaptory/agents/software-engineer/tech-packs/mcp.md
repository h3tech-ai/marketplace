# Tech Pack — MCP (Model Context Protocol)

> **Auto-loaded when:** MCP-related patterns detected in the project (MCP server, tool definitions, stdio/SSE transport). This tech pack provides implementation guidance for building MCP servers and integrations.

---

## MCP Overview

The Model Context Protocol (MCP) enables AI assistants to interact with external tools, data sources, and services through a standardized interface. MCP servers expose **tools**, **resources**, and **prompts** that AI clients can discover and use.

---

## Transport Patterns

### stdio (Default — Local Tools)

Best for: CLI tools, local integrations, development tools

```typescript
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";

const server = new Server({
  name: "my-server",
  version: "1.0.0",
}, {
  capabilities: {
    tools: {},
    resources: {},
  },
});

// Tool definitions
server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "tool_name",
      description: "What this tool does",
      inputSchema: {
        type: "object",
        properties: {
          param1: { type: "string", description: "Parameter description" },
        },
        required: ["param1"],
      },
    },
  ],
}));

// Tool execution
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  switch (name) {
    case "tool_name":
      const result = await doWork(args.param1);
      return { content: [{ type: "text", text: JSON.stringify(result) }] };
    default:
      throw new McpError(ErrorCode.MethodNotFound, `Unknown tool: ${name}`);
  }
});

const transport = new StdioServerTransport();
await server.connect(transport);
```

### SSE (Server-Sent Events — Remote Services)

Best for: Web-hosted tools, remote APIs, shared services

```typescript
import { SSEServerTransport } from "@modelcontextprotocol/sdk/server/sse.js";
import express from "express";

const app = express();

app.get("/sse", async (req, res) => {
  const transport = new SSEServerTransport("/messages", res);
  await server.connect(transport);
});

app.post("/messages", async (req, res) => {
  // Handle incoming messages
  await transport.handlePostMessage(req, res);
});

app.listen(3001);
```

### Streamable HTTP (Modern — Replaces SSE)

Best for: New implementations, bidirectional communication

```typescript
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";

const transport = new StreamableHTTPServerTransport({
  sessionIdGenerator: () => crypto.randomUUID(),
});

app.post("/mcp", async (req, res) => {
  await transport.handleRequest(req, res);
});
```

---

## Tool Design Patterns

### Tool Definition Best Practices

```typescript
{
  name: "search_documents",  // snake_case, verb_noun
  description: "Search documents by keyword query. Returns matching documents with relevance scores. Use when the user wants to find specific information in their document collection.",
  inputSchema: {
    type: "object",
    properties: {
      query: {
        type: "string",
        description: "Search query — supports keywords and phrases",
      },
      limit: {
        type: "number",
        description: "Maximum results to return (default: 10, max: 100)",
        default: 10,
      },
      filters: {
        type: "object",
        properties: {
          date_after: { type: "string", format: "date" },
          file_type: { type: "string", enum: ["pdf", "doc", "txt", "md"] },
        },
      },
    },
    required: ["query"],
  },
}
```

**Naming conventions:**
- `verb_noun`: `search_documents`, `create_issue`, `get_status`
- Group related tools: `db_query`, `db_insert`, `db_update`
- Avoid generic names: `do_thing` → `send_email`

**Description guidelines:**
- First sentence: what it does
- Second sentence: what it returns
- Third sentence (optional): when to use it
- Include limitations and edge cases

### Error Handling

```typescript
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  try {
    const result = await executeToolLogic(request.params);
    return {
      content: [{ type: "text", text: JSON.stringify(result) }],
    };
  } catch (error) {
    // Return errors as content, not exceptions
    // This lets the AI client understand and recover
    return {
      content: [{
        type: "text",
        text: JSON.stringify({
          error: true,
          message: error.message,
          suggestion: "Try with different parameters",
        }),
      }],
      isError: true,
    };
  }
});
```

### Resource Patterns

```typescript
server.setRequestHandler(ListResourcesRequestSchema, async () => ({
  resources: [
    {
      uri: "file:///config/settings.json",
      name: "Application Settings",
      description: "Current application configuration",
      mimeType: "application/json",
    },
  ],
}));

server.setRequestHandler(ReadResourceRequestSchema, async (request) => {
  const { uri } = request.params;
  const content = await readResource(uri);
  return {
    contents: [{
      uri,
      mimeType: "application/json",
      text: JSON.stringify(content),
    }],
  };
});
```

---

## Project Structure

```
mcp-server/
├── src/
│   ├── index.ts              # Entry point, transport setup
│   ├── server.ts             # Server definition, capability registration
│   ├── tools/                # Tool handlers (one file per tool group)
│   │   ├── index.ts          # Tool registry
│   │   ├── search.ts
│   │   └── crud.ts
│   ├── resources/            # Resource handlers
│   │   └── index.ts
│   ├── prompts/              # Prompt templates (optional)
│   │   └── index.ts
│   └── utils/                # Shared utilities
│       ├── validation.ts
│       └── errors.ts
├── tests/
│   ├── tools/
│   └── integration/
├── package.json
├── tsconfig.json
└── README.md
```

---

## Security Considerations

### Input Validation
- Validate ALL tool inputs with Zod or JSON Schema
- Sanitize file paths (prevent path traversal)
- Rate limit tool calls in SSE/HTTP transport
- Never execute raw user input as commands

### Authentication (SSE/HTTP only)
- Use bearer tokens or API keys for remote MCP servers
- Validate tokens in middleware, before transport handles the request
- Rotate credentials regularly

### Secrets Management
- Never hardcode API keys in tool implementations
- Use environment variables for all external service credentials
- Never return secrets in tool responses

---

## Testing Patterns

```typescript
// Unit test — tool handler
describe("search_documents", () => {
  it("returns matching documents", async () => {
    const result = await handleCallTool({
      params: {
        name: "search_documents",
        arguments: { query: "typescript", limit: 5 },
      },
    });
    expect(result.content[0].type).toBe("text");
    const data = JSON.parse(result.content[0].text);
    expect(data.results.length).toBeLessThanOrEqual(5);
  });
});

// Integration test — full server
describe("MCP server", () => {
  let client: Client;

  beforeAll(async () => {
    const transport = new InMemoryTransport();
    await server.connect(transport.server);
    client = new Client({ name: "test", version: "1.0" });
    await client.connect(transport.client);
  });

  it("lists tools", async () => {
    const tools = await client.listTools();
    expect(tools.tools.length).toBeGreaterThan(0);
  });

  it("calls tool and returns result", async () => {
    const result = await client.callTool({
      name: "search_documents",
      arguments: { query: "test" },
    });
    expect(result.isError).toBeFalsy();
  });
});
```

---

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Throwing exceptions from tool handlers | Return errors as content with `isError: true` |
| Large response payloads (>1MB) | Paginate or summarize results |
| Missing tool descriptions | Every tool needs a clear, multi-sentence description |
| Blocking the event loop | Use async operations for I/O-bound work |
| No input validation | Validate with Zod before processing |
| Stateful tools without session management | Use transport session IDs or make tools stateless |
| Exposing internal errors to clients | Wrap errors with user-friendly messages |
