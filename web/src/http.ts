/**
 * Local/development identity. Synthetic identity is a deliberate test-only
 * entry point on the server: the backend rejects it unless it was explicitly
 * enabled, and a configured OIDC verifier disables it entirely.
 */
export interface Identity {
  tenantId: string;
  subjectId: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string) {
    super(`请求失败 (${status} ${code})`);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }

  /** 401/403 will not become 200 by retrying; the caller must stop. */
  get isFatal(): boolean {
    return this.status === 401 || this.status === 403;
  }
}

export function identityHeaders(identity: Identity, extra?: HeadersInit): Headers {
  const headers = new Headers(extra);
  headers.set("X-Synthetic-Tenant", identity.tenantId);
  headers.set("X-Synthetic-Subject", identity.subjectId);
  return headers;
}

export async function toError(response: Response): Promise<ApiError> {
  let code = response.statusText;
  try {
    const body: unknown = await response.json();
    if (body !== null && typeof body === "object" && "detail" in body) {
      code = String((body as { detail: unknown }).detail);
    }
  } catch {
    // A non-JSON error body keeps the HTTP status text.
  }
  return new ApiError(response.status, code);
}

export async function request<T>(
  identity: Identity,
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: identityHeaders(identity, init?.headers),
  });
  if (!response.ok) {
    throw await toError(response);
  }
  return (await response.json()) as T;
}
