// Minimal type declarations for Node built-ins used by the trusted-worker runtime.
// Keeps `tsc -p .` self-contained without requiring node_modules/@types/node.

declare namespace NodeJS {
  interface ProcessEnv {
    [key: string]: string | undefined
  }
  interface Process {
    env: ProcessEnv
    platform: string
    cwd(): string
  }
}

declare var process: NodeJS.Process

declare module 'node:os' {
  export function homedir(): string
}

declare module 'node:path' {
  export function basename(path: string, suffix?: string): string
  export function dirname(path: string): string
  export function join(...paths: string[]): string
  export function resolve(...paths: string[]): string
  export function relative(from: string, to: string): string
  export const sep: string
}

declare module 'node:fs/promises' {
  export interface Dirent {
    name: string
    isDirectory(): boolean
    isFile(): boolean
    isSymbolicLink(): boolean
  }
  export interface Stats {
    size: number
    mtimeMs: number
    isDirectory(): boolean
    isFile(): boolean
  }
  export interface FileHandle {
    stat(): Promise<Stats>
    read(buffer: Uint8Array, offset: number, length: number, position: number): Promise<{ bytesRead: number }>
    close(): Promise<void>
  }
  export function access(path: string, mode?: number): Promise<void>
  export function open(path: string, flags: string): Promise<FileHandle>
  export function readdir(path: string, options: { withFileTypes: true }): Promise<Dirent[]>
  export function readdir(path: string): Promise<string[]>
  export function readFile(path: string, encoding: 'utf8'): Promise<string>
  export function realpath(path: string): Promise<string>
  export function stat(path: string): Promise<Stats>
  export function lstat(path: string): Promise<Stats>
  export function writeFile(path: string, data: string, encoding?: 'utf8'): Promise<void>
  export function unlink(path: string): Promise<void>
}

declare module 'node:child_process' {
  export interface ExecFileOptions {
    cwd?: string
    env?: Record<string, string | undefined>
    timeout?: number
    maxBuffer?: number
    encoding?: 'utf8'
  }
  export interface ExecFileException extends Error {
    code?: number | string | null
    killed?: boolean
    signal?: string | null
    stdout?: string
    stderr?: string
  }
  export function execFile(
    file: string,
    args: readonly string[],
    options: ExecFileOptions,
    callback: (error: ExecFileException | null, stdout: string, stderr: string) => void
  ): unknown
}
