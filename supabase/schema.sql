-- StreetScope cloud handoff schema.
-- Run this in Supabase SQL Editor after creating a project.

create extension if not exists pgcrypto;

create table if not exists public.streetscope_projects (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null default '未命名项目',
  config jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.streetscope_tasks (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  project_id uuid references public.streetscope_projects(id) on delete set null,
  kind text not null check (kind in ('download', 'metrics', 'download_then_metrics', 'uploaded_metrics')),
  status text not null default 'queued' check (status in ('queued', 'running', 'completed', 'failed', 'canceled')),
  payload jsonb not null default '{}'::jsonb,
  progress integer not null default 0,
  total integer not null default 0,
  succeeded integer not null default 0,
  failed integer not null default 0,
  message text not null default '',
  local_download_task_id text,
  local_metrics_task_id text,
  artifact_bucket text,
  artifact_path text,
  artifact_size_bytes bigint,
  records_preview jsonb not null default '[]'::jsonb,
  worker_id text,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  started_at timestamptz,
  completed_at timestamptz
);

create index if not exists streetscope_projects_user_idx on public.streetscope_projects(user_id, updated_at desc);
create index if not exists streetscope_tasks_user_idx on public.streetscope_tasks(user_id, created_at desc);
create index if not exists streetscope_tasks_queue_idx on public.streetscope_tasks(status, created_at asc);

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists streetscope_projects_updated_at on public.streetscope_projects;
create trigger streetscope_projects_updated_at
before update on public.streetscope_projects
for each row execute function public.set_updated_at();

drop trigger if exists streetscope_tasks_updated_at on public.streetscope_tasks;
create trigger streetscope_tasks_updated_at
before update on public.streetscope_tasks
for each row execute function public.set_updated_at();

alter table public.streetscope_projects enable row level security;
alter table public.streetscope_tasks enable row level security;

drop policy if exists "Users can read their StreetScope projects" on public.streetscope_projects;
create policy "Users can read their StreetScope projects"
on public.streetscope_projects for select
using (auth.uid() = user_id);

drop policy if exists "Users can create their StreetScope projects" on public.streetscope_projects;
create policy "Users can create their StreetScope projects"
on public.streetscope_projects for insert
with check (auth.uid() = user_id);

drop policy if exists "Users can update their StreetScope projects" on public.streetscope_projects;
create policy "Users can update their StreetScope projects"
on public.streetscope_projects for update
using (auth.uid() = user_id)
with check (auth.uid() = user_id);

drop policy if exists "Users can delete their StreetScope projects" on public.streetscope_projects;
create policy "Users can delete their StreetScope projects"
on public.streetscope_projects for delete
using (auth.uid() = user_id);

drop policy if exists "Users can read their StreetScope tasks" on public.streetscope_tasks;
create policy "Users can read their StreetScope tasks"
on public.streetscope_tasks for select
using (auth.uid() = user_id);

drop policy if exists "Users can create their StreetScope tasks" on public.streetscope_tasks;
create policy "Users can create their StreetScope tasks"
on public.streetscope_tasks for insert
with check (auth.uid() = user_id);

drop policy if exists "Users can update their queued StreetScope tasks" on public.streetscope_tasks;
create policy "Users can update their queued StreetScope tasks"
on public.streetscope_tasks for update
using (auth.uid() = user_id and status in ('queued', 'canceled'))
with check (auth.uid() = user_id);

insert into storage.buckets (id, name, public, file_size_limit)
values ('streetscope-artifacts', 'streetscope-artifacts', false, 10737418240)
on conflict (id) do nothing;

drop policy if exists "Users can read their StreetScope artifacts" on storage.objects;
create policy "Users can read their StreetScope artifacts"
on storage.objects for select
using (
  bucket_id = 'streetscope-artifacts'
  and auth.uid()::text = (storage.foldername(name))[1]
);

drop policy if exists "Users can upload their StreetScope artifacts" on storage.objects;
create policy "Users can upload their StreetScope artifacts"
on storage.objects for insert
with check (
  bucket_id = 'streetscope-artifacts'
  and auth.uid()::text = (storage.foldername(name))[1]
);

