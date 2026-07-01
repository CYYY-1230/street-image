import { useEffect, useMemo, useRef, useState } from 'react'
import { CircleMarker, MapContainer, Marker, Polyline, Rectangle, TileLayer, useMap, useMapEvents } from 'react-leaflet'
import L from 'leaflet'
import type { LatLngBoundsExpression, LatLngExpression, LeafletEvent, LeafletMouseEvent } from 'leaflet'
import {
  Activity,
  Archive,
  CheckCircle2,
  Cloud,
  Download,
  FileDown,
  FileUp,
  FolderOpen,
  Home,
  Image,
  KeyRound,
  Layers,
  Loader2,
  LogOut,
  MapPinned,
  Plus,
  Route,
  Settings2,
  Sparkles,
  UserRound,
} from 'lucide-react'
import 'leaflet/dist/leaflet.css'
import './App.css'
import { isSupabaseConfigured, supabase } from './supabaseClient'

const DEFAULT_API_BASE =
  import.meta.env.VITE_API_BASE ?? 'http://192.168.31.56:8000'
const DEFAULT_SEGMENTATION_SERVICE_URL = import.meta.env.VITE_DEFAULT_SEGMENTATION_SERVICE_URL ?? ''
const PROJECT_DRAFT_KEY = 'streetscope.projectDraft.v1'
const LOCAL_API_BASE_KEY = 'streetscope.localApiBase.v1'
const LEGACY_LOCAL_API_BASE = 'http://127.0.0.1:8000'

type Boundary = {
  north: number
  south: number
  east: number
  west: number
}

type SamplePoint = {
  point_id: string
  lng: number
  lat: number
  coord_type?: string
  lng_wgs84?: number
  lat_wgs84?: number
  lng_gcj02?: number
  lat_gcj02?: number
  lng_bd09: number
  lat_bd09: number
  road_id: string
  road_name: string
  admin_code?: string
  admin_name: string
  sample_interval: number
  source: string
  created_at?: string
}

type RoadFeature = {
  road_id: string
  road_name: string
  coordinates: [number, number][]
}

type SampleResponse = {
  points: SamplePoint[]
  roads: RoadFeature[]
  estimate: {
    area_km2: number
    road_length_km: number
    sample_points: number
    four_direction_images: number
    [key: string]: number
  }
  source: string
}

type TaskState = {
  task_id: string
  kind: 'download' | 'metrics'
  status: 'queued' | 'running' | 'paused' | 'completed' | 'failed' | 'canceled'
  progress: number
  total: number
  succeeded: number
  failed: number
  message: string
  records: Record<string, string | number | boolean>[]
  created_at?: string
  export_url?: string
  project_name?: string
  record_count?: number
}

type ProjectSummary = {
  project_name: string
  task_count: number
  download_count: number
  metrics_count: number
  completed_count: number
  latest_status: string
  latest_task_id: string
  latest_created_at: string
  latest_export_url: string
}

type CloudTask = {
  id: string
  user_id: string
  project_id: string | null
  kind: 'download' | 'metrics' | 'download_then_metrics' | 'uploaded_metrics'
  status: 'queued' | 'running' | 'completed' | 'failed' | 'canceled'
  payload?: Record<string, unknown> | null
  progress: number
  total: number
  succeeded: number
  failed: number
  message: string
  local_download_task_id?: string | null
  local_metrics_task_id?: string | null
  artifact_bucket?: string | null
  artifact_path?: string | null
  artifact_size_bytes?: number | null
  error?: string | null
  created_at: string
  updated_at: string
}

type ProjectConfig = {
  version: 1
  projectName: string
  boundary: Boundary
  intervalM: number
  roadDensity: 'low' | 'medium' | 'high'
  headings: number[]
  imageWidth: number
  imageHeight: number
  pitch: number
  fov: number
  coordtype: 'bd09ll' | 'wgs84ll' | 'gcj02'
  useRealBaidu: boolean
  downloadProvider?: 'baidu' | 'baidu_web'
  boundaryVisible?: boolean
  imageMode?: 'directions' | 'stitched' | 'panorama' | 'both'
  skipExisting?: boolean
  concurrency?: number
  retryCount?: number
  modelName: string
  selectedMetrics?: string[]
  inferenceMode?: 'external'
  segmentationServiceUrl?: string
  cloudProjectId?: string | null
  localApiBase?: string
  sample: SampleResponse | null
}

const modelArchitectures = ['Mask2Former', 'FCN', 'PSPNet', 'DeepLabv3'] as const
const modelDatasets = ['ADE20K', 'Cityscapes'] as const
const deployedModelNames = new Set(modelArchitectures.flatMap((architecture) => modelDatasets.map((dataset) => `${architecture} + ${dataset}`)))

function modelParts(modelName: string) {
  const [architecture, dataset] = modelName.split(' + ')
  return {
    architecture: modelArchitectures.includes(architecture as (typeof modelArchitectures)[number]) ? architecture : 'Mask2Former',
    dataset: modelDatasets.includes(dataset as (typeof modelDatasets)[number]) ? dataset : 'ADE20K',
  }
}

function artifactPaths(task: CloudTask): string[] {
  const rawPath = task.artifact_path?.trim()
  if (!rawPath) return []
  if (!rawPath.startsWith('[')) return [rawPath]
  try {
    const parsed = JSON.parse(rawPath)
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === 'string' && item.length > 0) : [rawPath]
  } catch {
    return [rawPath]
  }
}

function readableCloudTaskKind(kind: CloudTask['kind']) {
  if (kind === 'download_then_metrics') return '完整生产任务'
  if (kind === 'download') return '街景下载'
  if (kind === 'metrics') return '语义分割'
  return '上传图片分割'
}

function isCloudTaskActive(task: CloudTask) {
  return task.status === 'queued' || task.status === 'running'
}

function readableCloudError(task: CloudTask) {
  const raw = `${task.message || ''} ${task.error || ''}`.trim()
  if (!raw) return '等待更新'
  if (task.status !== 'failed') return task.message || task.error || '等待更新'
  if (raw.includes('Payload too large') || raw.includes('413')) {
    return '结果 ZIP 太大，旧版 Worker 上传失败；请更新 NAS Worker 后重试'
  }
  if (raw.includes('/health') || raw.includes('模型服务不可用')) {
    return '模型服务不可用，请确认 GPU 服务已开机且 /health 可访问'
  }
  if (raw.includes('RemoteDisconnected') || raw.includes('Connection aborted')) {
    return '网络连接中断，请稍后重试；已下载内容会复用'
  }
  if (raw.includes('没有找到可用于真实分割的图片')) {
    return '没有可用于分割的街景图像，请先完成街景下载'
  }
  return task.error || task.message || 'Worker 执行失败'
}

const adminPresets: Record<string, Boundary> = {
  徐汇示范区: { north: 31.2006, south: 31.1906, east: 121.4485, west: 121.4355 },
  上海徐汇区: { north: 31.2208, south: 31.1392, east: 121.4976, west: 121.3972 },
  北京海淀区: { north: 40.1052, south: 39.8725, east: 116.4074, west: 116.1752 },
  广州天河区: { north: 23.1915, south: 23.0834, east: 113.4319, west: 113.2915 },
  杭州西湖区: { north: 30.3335, south: 30.1669, east: 120.1901, west: 120.0302 },
}

const workflowSteps = ['研究区', '采样点', '街景图', '语义分割', '指标导出']
const defaultSelectedMetrics = ['gvi', 'bvi', 'sky_ratio', 'building_ratio', 'road_ratio', 'sidewalk_ratio', 'visual_entropy']
const metricOptions = [
  { key: 'gvi', label: '绿视率' },
  { key: 'bvi', label: '蓝视率' },
  { key: 'sky_ratio', label: '天空开阔度' },
  { key: 'water_ratio', label: '水体占比' },
  { key: 'building_ratio', label: '建筑占比' },
  { key: 'road_ratio', label: '道路占比' },
  { key: 'sidewalk_ratio', label: '人行空间占比' },
  { key: 'vehicle_space_ratio', label: '车行空间占比' },
  { key: 'hardscape_ratio', label: '硬质铺装占比' },
  { key: 'human_vehicle_density', label: '人车密度' },
  { key: 'visual_entropy', label: '视觉熵' },
  { key: 'natural_ratio', label: '自然度' },
  { key: 'enclosure_ratio', label: '界面围合度' },
  { key: 'cvi', label: '色彩丰富度' },
]

function boundsToLeaflet(boundary: Boundary): LatLngBoundsExpression {
  return [
    [boundary.south, boundary.west],
    [boundary.north, boundary.east],
  ]
}

function MapClickSetter({ onPick }: { onPick: (lat: number, lng: number) => void }) {
  useMapEvents({
    click(event) {
      onPick(event.latlng.lat, event.latlng.lng)
    },
  })
  return null
}

function boundaryFromCorners(start: LatLngExpression, end: LatLngExpression): Boundary {
  const [startLat, startLng] = start as [number, number]
  const [endLat, endLng] = end as [number, number]
  return {
    north: Number(Math.max(startLat, endLat).toFixed(6)),
    south: Number(Math.min(startLat, endLat).toFixed(6)),
    east: Number(Math.max(startLng, endLng).toFixed(6)),
    west: Number(Math.min(startLng, endLng).toFixed(6)),
  }
}

function BoundaryDrawTool({
  active,
  onPreview,
  onDone,
}: {
  active: boolean
  onPreview: (boundary: Boundary) => void
  onDone: (boundary: Boundary) => void
}) {
  const map = useMap()
  const [start, setStart] = useState<LatLngExpression | null>(null)
  useEffect(() => {
    if (active) {
      map.dragging.disable()
      map.getContainer().classList.add('drawing-boundary')
    } else {
      map.dragging.enable()
      map.getContainer().classList.remove('drawing-boundary')
      setStart(null)
    }
    return () => {
      map.dragging.enable()
      map.getContainer().classList.remove('drawing-boundary')
    }
  }, [active, map])
  useMapEvents({
    mousedown(event: LeafletMouseEvent) {
      if (!active) return
      setStart([event.latlng.lat, event.latlng.lng])
    },
    mousemove(event: LeafletMouseEvent) {
      if (!active || !start) return
      onPreview(boundaryFromCorners(start, [event.latlng.lat, event.latlng.lng]))
    },
    mouseup(event: LeafletMouseEvent) {
      if (!active || !start) return
      onDone(boundaryFromCorners(start, [event.latlng.lat, event.latlng.lng]))
      setStart(null)
    },
  })
  return null
}

function FitBoundary({ boundary, version }: { boundary: Boundary; version: number }) {
  const map = useMap()
  const boundaryRef = useRef(boundary)
  useEffect(() => {
    boundaryRef.current = boundary
  }, [boundary])
  useEffect(() => {
    if (version <= 0) return
    map.fitBounds(boundsToLeaflet(boundaryRef.current), { padding: [20, 20], animate: true })
  }, [version, map])
  return null
}

const boundaryHandleIcon = L.divIcon({
  className: 'boundary-handle',
  html: '<span></span>',
  iconSize: [18, 18],
  iconAnchor: [9, 9],
})

const boundaryMoveIcon = L.divIcon({
  className: 'boundary-handle boundary-move-handle',
  html: '<span></span>',
  iconSize: [22, 22],
  iconAnchor: [11, 11],
})

function EditableBoundary({ boundary, onChange }: { boundary: Boundary; onChange: (boundary: Boundary) => void }) {
  const minSpan = 0.0008
  const centerLat = (boundary.north + boundary.south) / 2
  const centerLng = (boundary.west + boundary.east) / 2
  const handles = [
    { id: 'nw', lat: boundary.north, lng: boundary.west },
    { id: 'n', lat: boundary.north, lng: (boundary.west + boundary.east) / 2 },
    { id: 'ne', lat: boundary.north, lng: boundary.east },
    { id: 'e', lat: (boundary.north + boundary.south) / 2, lng: boundary.east },
    { id: 'se', lat: boundary.south, lng: boundary.east },
    { id: 's', lat: boundary.south, lng: (boundary.west + boundary.east) / 2 },
    { id: 'sw', lat: boundary.south, lng: boundary.west },
    { id: 'w', lat: (boundary.north + boundary.south) / 2, lng: boundary.west },
    { id: 'center', lat: centerLat, lng: centerLng },
  ]
  const updateFromHandle = (id: string, event: LeafletEvent) => {
    const marker = event.target as L.Marker
    const { lat, lng } = marker.getLatLng()
    if (id === 'center') {
      const deltaLat = lat - centerLat
      const deltaLng = lng - centerLng
      onChange({
        north: Number((boundary.north + deltaLat).toFixed(6)),
        south: Number((boundary.south + deltaLat).toFixed(6)),
        east: Number((boundary.east + deltaLng).toFixed(6)),
        west: Number((boundary.west + deltaLng).toFixed(6)),
      })
      return
    }
    const next = { ...boundary }
    if (id.includes('n')) next.north = Math.max(lat, boundary.south + minSpan)
    if (id.includes('s')) next.south = Math.min(lat, boundary.north - minSpan)
    if (id.includes('e')) next.east = Math.max(lng, boundary.west + minSpan)
    if (id.includes('w')) next.west = Math.min(lng, boundary.east - minSpan)
    onChange({
      north: Number(next.north.toFixed(6)),
      south: Number(next.south.toFixed(6)),
      east: Number(next.east.toFixed(6)),
      west: Number(next.west.toFixed(6)),
    })
  }
  return (
    <>
      <Rectangle bounds={boundsToLeaflet(boundary)} pathOptions={{ color: '#0f766e', weight: 2, fillOpacity: 0.08 }} />
      {handles.map((handle) => (
        <Marker
          key={handle.id}
          position={[handle.lat, handle.lng]}
          icon={handle.id === 'center' ? boundaryMoveIcon : boundaryHandleIcon}
          draggable
          eventHandlers={{ drag: (event) => updateFromHandle(handle.id, event), dragend: (event) => updateFromHandle(handle.id, event) }}
        />
      ))}
    </>
  )
}

function formatNumber(value: number) {
  return new Intl.NumberFormat('zh-CN').format(value)
}

function gviColor(gvi: number) {
  if (gvi >= 0.45) return '#15803d'
  if (gvi >= 0.3) return '#65a30d'
  if (gvi >= 0.18) return '#f59e0b'
  return '#dc2626'
}

function normalizeApiBase(value?: string | null) {
  const trimmed = value?.trim().replace(/\/+$/, '')
  if (!trimmed || trimmed === LEGACY_LOCAL_API_BASE) return DEFAULT_API_BASE
  return trimmed
}

function readableApiError(err: unknown, fallback: string) {
  if (err instanceof TypeError && err.message === 'Failed to fetch') {
    return `${fallback}：无法连接本地/NAS 数据服务。请确认 NAS 容器正在运行，服务地址可访问，并且当前电脑和 NAS 在同一网络。`
  }
  if (err instanceof Error) return err.message
  return fallback
}

async function apiRequest<T>(apiBase: string, path: string, options?: RequestInit): Promise<T> {
  const base = normalizeApiBase(apiBase)
  if (!base) throw new Error('请先配置本地/NAS 数据服务地址')
  const response = await fetch(`${base}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options?.headers ?? {}) },
    ...options,
  })
  if (!response.ok) {
    const detail = await response.text()
    throw new Error(detail || `HTTP ${response.status}`)
  }
  return response.json() as Promise<T>
}

async function uploadRequest<T>(apiBase: string, path: string, formData: FormData): Promise<T> {
  const base = normalizeApiBase(apiBase)
  if (!base) throw new Error('请先配置本地/NAS 数据服务地址')
  const response = await fetch(`${base}${path}`, {
    method: 'POST',
    body: formData,
  })
  if (!response.ok) {
    const detail = await response.text()
    throw new Error(detail || `HTTP ${response.status}`)
  }
  return response.json() as Promise<T>
}

function App() {
  const [view, setView] = useState<'home' | 'workspace'>('home')
  const [projectName, setProjectName] = useState('徐汇示范区街景绿视率研究')
  const [activeStep, setActiveStep] = useState(0)
  const [boundary, setBoundary] = useState<Boundary>(adminPresets.徐汇示范区)
  const [boundaryVisible, setBoundaryVisible] = useState(false)
  const [drawingBoundary, setDrawingBoundary] = useState(false)
  const [fitBoundaryVersion, setFitBoundaryVersion] = useState(0)
  const [intervalM, setIntervalM] = useState(100)
  const [roadDensity, setRoadDensity] = useState<'low' | 'medium' | 'high'>('medium')
  const [osmWalkableOnly, setOsmWalkableOnly] = useState(true)
  const [osmExcludeHighSpeed, setOsmExcludeHighSpeed] = useState(true)
  const [cleanRoads, setCleanRoads] = useState(true)
  const [sample, setSample] = useState<SampleResponse | null>(null)
  const [sampling, setSampling] = useState(false)
  const [ak, setAk] = useState('')
  const [downloadProvider, setDownloadProvider] = useState<'baidu' | 'baidu_web'>('baidu_web')
  const [useRealBaidu, setUseRealBaidu] = useState(false)
  const [headings, setHeadings] = useState([0, 90, 180, 270])
  const [imageMode, setImageMode] = useState<'directions' | 'stitched' | 'panorama'>('directions')
  const [skipExisting, setSkipExisting] = useState(true)
  const [concurrency, setConcurrency] = useState(2)
  const [retryCount, setRetryCount] = useState(1)
  const [imageWidth, setImageWidth] = useState(1024)
  const [imageHeight, setImageHeight] = useState(512)
  const [pitch, setPitch] = useState(0)
  const [fov, setFov] = useState(90)
  const [coordtype, setCoordtype] = useState<'bd09ll' | 'wgs84ll' | 'gcj02'>('bd09ll')
  const [testingKey, setTestingKey] = useState(false)
  const [keyTestMessage, setKeyTestMessage] = useState('')
  const [downloadTask, setDownloadTask] = useState<TaskState | null>(null)
  const [metricsTask, setMetricsTask] = useState<TaskState | null>(null)
  const [modelName, setModelName] = useState('Mask2Former + ADE20K')
  const [selectedMetrics, setSelectedMetrics] = useState(defaultSelectedMetrics)
  const [inferenceMode] = useState<'external'>('external')
  const [segmentationServiceUrl, setSegmentationServiceUrl] = useState(DEFAULT_SEGMENTATION_SERVICE_URL)
  const [error, setError] = useState('')
  const [draftStatus, setDraftStatus] = useState('未保存')
  const [draftReady, setDraftReady] = useState(false)
  const [recentProjects, setRecentProjects] = useState<ProjectSummary[]>([])
  const [cloudUser, setCloudUser] = useState<{ id: string; email?: string } | null>(null)
  const [cloudEmail, setCloudEmail] = useState('')
  const [cloudPassword, setCloudPassword] = useState('')
  const [cloudAuthMessage, setCloudAuthMessage] = useState('')
  const [cloudTasks, setCloudTasks] = useState<CloudTask[]>([])
  const [cloudProjectId, setCloudProjectId] = useState<string | null>(null)
  const [cloudSubmitting, setCloudSubmitting] = useState(false)
  const [localApiBase, setLocalApiBase] = useState(() => {
    const savedBase = window.localStorage.getItem(LOCAL_API_BASE_KEY)
    return normalizeApiBase(savedBase)
  })
  const [localApiStatus, setLocalApiStatus] = useState<'unknown' | 'checking' | 'ok' | 'failed'>('unknown')
  const [localApiMessage, setLocalApiMessage] = useState('')
  const cloudSubmitLock = useRef(false)

  const cloudReady = isSupabaseConfigured && Boolean(supabase) && Boolean(cloudUser)
  const backendBase = normalizeApiBase(localApiBase)
  const localBackendEnabled = backendBase.length > 0
  const useCloudQueue = import.meta.env.PROD && cloudReady
  const api = <T,>(path: string, options?: RequestInit) => apiRequest<T>(backendBase, path, options)
  const uploadApi = <T,>(path: string, formData: FormData) => uploadRequest<T>(backendBase, path, formData)

  const mapCenter: LatLngExpression = useMemo(
    () => [(boundary.north + boundary.south) / 2, (boundary.east + boundary.west) / 2],
    [boundary],
  )

  const previewPoints = useMemo(() => sample?.points.slice(0, 260) ?? [], [sample])
  const metricPreviewPoints = useMemo(() => {
    if (!sample || !metricsTask?.records.length) return []
    const pointLookup = new Map(sample.points.map((point) => [point.point_id, point]))
    const buckets = new Map<string, { total: number; count: number }>()
    for (const record of metricsTask.records) {
      const pointId = String(record.point_id ?? '')
      const gvi = Number(record.gvi ?? Number.NaN)
      if (!pointId || Number.isNaN(gvi)) continue
      const bucket = buckets.get(pointId) ?? { total: 0, count: 0 }
      bucket.total += gvi
      bucket.count += 1
      buckets.set(pointId, bucket)
    }
    return Array.from(buckets.entries())
      .map(([pointId, bucket]) => {
        const point = pointLookup.get(pointId)
        return point ? { ...point, gvi: bucket.total / Math.max(bucket.count, 1) } : null
      })
      .filter((point): point is SamplePoint & { gvi: number } => Boolean(point))
      .slice(0, 260)
  }, [metricsTask?.records, sample])

  const projectConfig = useMemo<ProjectConfig>(
    () => ({
      version: 1,
      projectName,
      boundary,
      boundaryVisible,
      intervalM,
      roadDensity,
      headings,
      imageWidth,
      imageHeight,
      pitch,
      fov,
      coordtype,
      useRealBaidu,
      downloadProvider,
      imageMode,
      skipExisting,
      concurrency,
      retryCount,
      modelName,
      selectedMetrics,
      inferenceMode,
      segmentationServiceUrl,
      cloudProjectId,
      localApiBase: backendBase,
      sample,
    }),
    [backendBase, boundary, boundaryVisible, cloudProjectId, concurrency, coordtype, downloadProvider, fov, headings, imageHeight, imageMode, imageWidth, inferenceMode, intervalM, modelName, pitch, projectName, retryCount, roadDensity, sample, segmentationServiceUrl, selectedMetrics, skipExisting, useRealBaidu],
  )

  const buildDownloadRequest = () => ({
    project_name: projectName,
    ak,
    use_real_baidu: downloadProvider === 'baidu',
    provider: downloadProvider,
    points: sample?.points ?? [],
    boundary,
    roads: sample?.roads ?? [],
    headings,
    image_mode: imageMode,
    skip_existing: skipExisting,
    concurrency,
    retry_count: retryCount,
    width: imageWidth,
    height: imageHeight,
    pitch,
    fov,
    coordtype: 'bd09ll',
  })

  const buildMetricsRequest = () => ({
    project_name: projectName,
    points: sample?.points ?? [],
    boundary,
    roads: sample?.roads ?? [],
    headings,
    source_download_task_id: '',
    model_name: modelName,
    selected_metrics: selectedMetrics,
    inference_mode: inferenceMode,
    segmentation_service_url: segmentationServiceUrl,
  })

  const ensureCloudProject = async () => {
    if (!supabase || !cloudUser) {
      throw new Error('公网模式需要先登录云端账号，再由 NAS Worker 领取任务。')
    }
    if (cloudProjectId) {
      await supabase
        .from('streetscope_projects')
        .update({
          name: projectName || '未命名项目',
          config: { ...projectConfig, cloudProjectId },
          updated_at: new Date().toISOString(),
        })
        .eq('id', cloudProjectId)
        .eq('user_id', cloudUser.id)
      return cloudProjectId
    }
    const { data: project, error: projectError } = await supabase
      .from('streetscope_projects')
      .insert({
        user_id: cloudUser.id,
        name: projectName || '未命名项目',
        config: projectConfig,
      })
      .select('id')
      .single()
    if (projectError) throw projectError
    setCloudProjectId(project.id)
    return project.id as string
  }

  const submitCloudTask = async (
    kind: CloudTask['kind'],
    payload: Record<string, unknown>,
    message: string,
    nextStep = 4,
  ) => {
    if (!supabase || !cloudUser) {
      throw new Error('公网模式需要先登录云端账号，再由 NAS Worker 领取任务。')
    }
    if (cloudSubmitLock.current) {
      throw new Error('任务正在提交，请不要重复点击。')
    }
    cloudSubmitLock.current = true
    setCloudSubmitting(true)
    try {
      const projectId = await ensureCloudProject()
      const { data: activeTasks, error: activeError } = await supabase
        .from('streetscope_tasks')
        .select('id, kind, status, progress, total, succeeded, failed, message, local_download_task_id, local_metrics_task_id, artifact_bucket, artifact_path, artifact_size_bytes, error, created_at, updated_at, project_id, user_id, payload')
        .eq('project_id', projectId)
        .eq('user_id', cloudUser.id)
        .in('status', ['queued', 'running'])
        .order('created_at', { ascending: false })
        .limit(1)
      if (activeError) throw activeError
      if (activeTasks?.length) {
        setCloudTasks(activeTasks as CloudTask[])
        setCloudAuthMessage('当前项目已有任务在排队或运行，已为你跳转到任务进度。')
        setActiveStep(nextStep)
        await refreshCloudTasks(projectId)
        return
      }
      const { error: taskError } = await supabase.from('streetscope_tasks').insert({
        user_id: cloudUser.id,
        project_id: projectId,
        kind,
        status: 'queued',
        payload,
        message,
      })
      if (taskError) throw taskError
      setCloudAuthMessage('云端任务已提交。NAS Worker 会自动领取、执行并上传结果。')
      setActiveStep(nextStep)
      await refreshCloudTasks(projectId)
    } finally {
      cloudSubmitLock.current = false
      setCloudSubmitting(false)
    }
  }

  const refreshCloudTasks = async (projectId = cloudProjectId) => {
    if (!supabase || !cloudUser) return
    if (!projectId) {
      setCloudTasks([])
      return
    }
    const { data, error: cloudError } = await supabase
      .from('streetscope_tasks')
      .select('id, user_id, project_id, kind, status, payload, progress, total, succeeded, failed, message, local_download_task_id, local_metrics_task_id, artifact_bucket, artifact_path, artifact_size_bytes, error, created_at, updated_at')
      .eq('project_id', projectId)
      .eq('user_id', cloudUser.id)
      .order('created_at', { ascending: false })
      .limit(20)
    if (cloudError) {
      setCloudAuthMessage(cloudError.message)
      return
    }
    setCloudTasks((data ?? []) as CloudTask[])
  }

  const signInCloud = async () => {
    if (!supabase) {
      setCloudAuthMessage('当前未配置 Supabase 环境变量。')
      return
    }
    if (!cloudEmail.trim() || !cloudPassword.trim()) {
      setCloudAuthMessage('请填写邮箱和密码。')
      return
    }
    setCloudAuthMessage('')
    const { error: signInError } = await supabase.auth.signInWithPassword({
      email: cloudEmail.trim(),
      password: cloudPassword,
    })
    if (signInError) setCloudAuthMessage(signInError.message)
  }

  const signUpCloud = async () => {
    if (!supabase) {
      setCloudAuthMessage('当前未配置 Supabase 环境变量。')
      return
    }
    if (!cloudEmail.trim() || !cloudPassword.trim()) {
      setCloudAuthMessage('请填写邮箱和密码。')
      return
    }
    setCloudAuthMessage('')
    const { error: signUpError } = await supabase.auth.signUp({
      email: cloudEmail.trim(),
      password: cloudPassword,
    })
    setCloudAuthMessage(signUpError ? signUpError.message : '注册请求已提交；如果 Supabase 开启邮箱验证，请先完成邮箱确认。')
  }

  const signOutCloud = async () => {
    if (!supabase) return
    await supabase.auth.signOut()
    setCloudUser(null)
    setCloudTasks([])
    setCloudProjectId(null)
  }

  const checkLocalApi = async (silent = false) => {
    if (!backendBase) {
      setLocalApiStatus('failed')
      setLocalApiMessage('未配置本地/NAS 数据服务地址')
      return false
    }
    if (!silent) {
      setLocalApiStatus('checking')
      setLocalApiMessage('正在检测 NAS 数据服务...')
    }
    try {
      const health = await api<{ status: string }>('/api/health')
      const ok = health.status === 'ok'
      setLocalApiStatus(ok ? 'ok' : 'failed')
      setLocalApiMessage(ok ? 'NAS 数据服务已连接' : 'NAS 数据服务返回异常')
      return ok
    } catch (err) {
      setLocalApiStatus('failed')
      setLocalApiMessage(readableApiError(err, 'NAS 数据服务检测失败'))
      return false
    }
  }

  const refreshOverview = async () => {
    if (!localBackendEnabled) return
    try {
      const projectResult = await api<{ projects: ProjectSummary[] }>('/api/projects')
      setRecentProjects(projectResult.projects)
    } catch {
      // Overview is best-effort; task creation and exports should not be blocked by it.
    }
  }

  const applyProjectConfig = (config: ProjectConfig, revealSavedBoundary = true) => {
    if (config.version !== 1) throw new Error('项目配置版本不兼容')
    const restoredBoundaryVisible = revealSavedBoundary && Boolean(config.boundaryVisible)
    setProjectName(config.projectName)
    setBoundary(config.boundary)
    setBoundaryVisible(restoredBoundaryVisible)
    if (restoredBoundaryVisible) setFitBoundaryVersion((value) => value + 1)
    setIntervalM(config.intervalM)
    setRoadDensity(config.roadDensity)
    setHeadings(config.headings)
    setImageWidth(config.imageWidth)
    setImageHeight(config.imageHeight)
    setPitch(config.pitch)
    setFov(config.fov)
    setCoordtype(config.coordtype)
    setUseRealBaidu(config.useRealBaidu)
    setDownloadProvider(config.downloadProvider === 'baidu' ? 'baidu' : 'baidu_web')
    setImageMode(config.imageMode === 'both' ? 'panorama' : (config.imageMode ?? 'directions'))
    setSkipExisting(config.skipExisting ?? true)
    setConcurrency(config.concurrency ?? 2)
    setRetryCount(config.retryCount ?? 1)
    setModelName(config.modelName)
    setSelectedMetrics(config.selectedMetrics?.length ? config.selectedMetrics : defaultSelectedMetrics)
    setSegmentationServiceUrl(config.segmentationServiceUrl ?? DEFAULT_SEGMENTATION_SERVICE_URL)
    setLocalApiBase(normalizeApiBase(config.localApiBase))
    setSample(config.sample)
    setCloudProjectId(config.cloudProjectId ?? null)
    if (!config.cloudProjectId) setCloudTasks([])
    setDownloadTask(null)
    setMetricsTask(null)
    setActiveStep(config.sample ? 1 : 0)
  }

  useEffect(() => {
    const saved = window.localStorage.getItem(PROJECT_DRAFT_KEY)
    if (!saved) {
      setDraftReady(true)
      return
    }
    try {
      applyProjectConfig(JSON.parse(saved) as ProjectConfig, false)
      setDraftStatus('已恢复本地草稿')
    } catch {
      setDraftStatus('草稿恢复失败')
    } finally {
      setDraftReady(true)
    }
  }, [])

  useEffect(() => {
    if (!draftReady) return
    try {
      window.localStorage.setItem(PROJECT_DRAFT_KEY, JSON.stringify(projectConfig))
      window.localStorage.setItem(LOCAL_API_BASE_KEY, backendBase)
      setDraftStatus('已自动保存')
    } catch {
      setDraftStatus('自动保存失败')
    }
  }, [draftReady, projectConfig])

  useEffect(() => {
    if (!draftReady || !backendBase) return
    checkLocalApi(true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftReady, backendBase])

  useEffect(() => {
    refreshOverview()
  }, [])

  useEffect(() => {
    if (!supabase) return undefined
    let alive = true
    supabase.auth.getSession().then(({ data }) => {
      if (!alive) return
      const user = data.session?.user
      setCloudUser(user ? { id: user.id, email: user.email ?? undefined } : null)
      if (user?.email) setCloudEmail(user.email)
    })
    const { data: listener } = supabase.auth.onAuthStateChange((_event, session) => {
      const user = session?.user
      setCloudUser(user ? { id: user.id, email: user.email ?? undefined } : null)
      if (user?.email) setCloudEmail(user.email)
    })
    return () => {
      alive = false
      listener.subscription.unsubscribe()
    }
  }, [])

  useEffect(() => {
    if (!cloudUser) {
      setCloudTasks([])
      return undefined
    }
    refreshCloudTasks()
    const timer = window.setInterval(refreshCloudTasks, 3000)
    return () => window.clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cloudUser?.id, cloudProjectId])

  useEffect(() => {
    const taskId = downloadTask?.task_id
    const status = downloadTask?.status
    if (!taskId || (status !== 'running' && status !== 'queued')) return
    const timer = window.setInterval(async () => {
      try {
        const next = await api<TaskState>(`/api/tasks/${taskId}`)
        setDownloadTask(next)
        refreshOverview()
        if (next.status === 'completed' || next.status === 'failed' || next.status === 'canceled' || next.status === 'paused') window.clearInterval(timer)
      } catch (err) {
        setError(readableApiError(err, '街景下载任务状态刷新失败'))
      }
    }, 900)
    return () => window.clearInterval(timer)
  }, [downloadTask?.task_id, downloadTask?.status])

  useEffect(() => {
    const taskId = metricsTask?.task_id
    const status = metricsTask?.status
    if (!taskId || (status !== 'running' && status !== 'queued')) return
    const timer = window.setInterval(async () => {
      try {
        const next = await api<TaskState>(`/api/tasks/${taskId}`)
        setMetricsTask(next)
        refreshOverview()
        if (next.status === 'completed' || next.status === 'failed' || next.status === 'canceled' || next.status === 'paused') window.clearInterval(timer)
      } catch (err) {
        setError(readableApiError(err, '语义指标任务状态刷新失败'))
      }
    }, 900)
    return () => window.clearInterval(timer)
  }, [metricsTask?.task_id, metricsTask?.status])

  const loadOsmRoads = async () => {
    if (!boundaryVisible) {
      setError('请先绘制或选择研究区边界')
      return
    }
    setError('')
    setSampling(true)
    try {
      const result = await api<SampleResponse>('/api/osm-roads', {
        method: 'POST',
        body: JSON.stringify({
          boundary,
          interval_m: intervalM,
          keep_walkable: osmWalkableOnly,
          exclude_high_speed: osmExcludeHighSpeed,
          clean_roads: cleanRoads,
        }),
      })
      setSample(result)
      setActiveStep(1)
    } catch (err) {
      setError(readableApiError(err, 'OSM 路网加载失败'))
    } finally {
      setSampling(false)
    }
  }

  const startDownload = async () => {
    if (!sample?.points.length) return
    setError('')
    if (useCloudQueue || !localBackendEnabled) {
      if (downloadProvider === 'baidu' && !ak.trim()) {
        setError('官方 API Key 下载需要先填写百度 AK。')
        return
      }
      try {
        await submitCloudTask('download', buildDownloadRequest(), '等待 NAS Worker 下载街景图像', 4)
      } catch (err) {
        setError(err instanceof Error ? err.message : '云端下载任务提交失败')
      }
      return
    }
    try {
      const { task_id } = await api<{ task_id: string }>('/api/download-task', {
        method: 'POST',
        body: JSON.stringify({
          project_name: projectName,
          ak,
          use_real_baidu: downloadProvider === 'baidu',
          provider: downloadProvider,
          points: sample.points,
          boundary,
          roads: sample.roads,
          headings,
          image_mode: imageMode,
          skip_existing: skipExisting,
          concurrency,
          retry_count: retryCount,
          width: imageWidth,
          height: imageHeight,
          pitch,
          fov,
          coordtype: 'bd09ll',
        }),
      })
      setDownloadTask({
        task_id,
        kind: 'download',
        status: 'queued',
        progress: 0,
        total: 0,
        succeeded: 0,
        failed: 0,
        message: '任务已提交',
        records: [],
      })
      setActiveStep(2)
      refreshOverview()
    } catch (err) {
      setError(readableApiError(err, '下载任务创建失败'))
    }
  }

  const testBaiduKey = async () => {
    setError('')
    setKeyTestMessage('')
    if (!localBackendEnabled) {
      setError('公网模式不直接测试百度 AK；请提交云端任务，由 NAS Worker 执行真实请求。')
      return
    }
    setTestingKey(true)
    try {
      const result = await api<{ ok: boolean; status_code: number; bytes: number; message: string }>('/api/baidu-test', {
        method: 'POST',
        body: JSON.stringify({
          ak,
          point: sample?.points[0],
          width: 512,
          height: 256,
          heading: headings[0] ?? 0,
          pitch,
          fov,
          coordtype,
        }),
      })
      setKeyTestMessage(result.ok ? `测试通过：返回 ${result.bytes} bytes 图像` : `测试未通过：${result.message}`)
    } catch (err) {
      setError(readableApiError(err, '百度 API Key 测试失败'))
    } finally {
      setTestingKey(false)
    }
  }

  const startMetrics = async () => {
    if (!sample?.points.length) return
    if (!localBackendEnabled) {
      setError('公网模式不直接运行本地分割任务。请使用“提交云端完整任务”，由 NAS Worker 下载街景、调用模型服务并上传最终 ZIP。')
      return
    }
    if (!deployedModelNames.has(modelName)) {
      setError(`${modelName} 还没有在云端部署对应权重。请先检查模型服务 /health。`)
      return
    }
    if (!finishedDownload) {
      setError('真实分割需要先完成街景图像下载任务，再用已下载图片计算指标。')
      return
    }
    if (!segmentationServiceUrl.trim()) {
      setError('真实分割需要填写模型服务地址。')
      return
    }
    setError('')
    try {
      const { task_id } = await api<{ task_id: string }>('/api/metrics-task', {
        method: 'POST',
        body: JSON.stringify({
          project_name: projectName,
          points: sample.points,
          boundary,
          roads: sample.roads,
          headings,
          source_download_task_id: downloadTask?.task_id ?? '',
          model_name: modelName,
          selected_metrics: selectedMetrics,
          inference_mode: inferenceMode,
          segmentation_service_url: segmentationServiceUrl,
        }),
      })
      setMetricsTask({
        task_id,
        kind: 'metrics',
        status: 'queued',
        progress: 0,
        total: 0,
        succeeded: 0,
        failed: 0,
        message: '任务已提交',
        records: [],
      })
      setActiveStep(3)
      refreshOverview()
    } catch (err) {
      setError(readableApiError(err, '指标任务创建失败'))
    }
  }

  const submitCloudFullTask = async () => {
    if (!supabase || !cloudUser) {
      setError('请先登录 Supabase 云端账号。')
      return
    }
    if (!sample?.points.length) {
      setError('请先生成采样点。')
      return
    }
    if (downloadProvider === 'baidu' && !ak.trim()) {
      setError('官方 API Key 下载需要先填写百度 AK。')
      return
    }
    if (!segmentationServiceUrl.trim()) {
      setError('云端完整任务需要填写模型服务地址。')
      return
    }
    setError('')
    try {
      await submitCloudTask(
        'download_then_metrics',
        {
          download_request: buildDownloadRequest(),
          metrics_request: buildMetricsRequest(),
        },
        '等待 NAS Worker 执行完整生产任务',
        4,
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : '云端任务提交失败')
    }
  }

  const downloadCloudArtifact = async (task: CloudTask, artifactPath: string) => {
    if (!supabase || !artifactPath) return
    const { data, error: signError } = await supabase.storage
      .from(task.artifact_bucket || 'streetscope-artifacts')
      .createSignedUrl(artifactPath, 60 * 10)
    if (signError) {
      setError(signError.message)
      return
    }
    window.open(data.signedUrl, '_blank', 'noopener,noreferrer')
  }

  const cancelQueuedCloudTask = async (task: CloudTask) => {
    if (!supabase || task.status !== 'queued') return
    setError('')
    const { error: cancelError } = await supabase
      .from('streetscope_tasks')
      .update({
        status: 'canceled',
        message: '用户已取消排队任务',
        error: 'canceled by user before worker pickup',
        updated_at: new Date().toISOString(),
      })
      .eq('id', task.id)
      .eq('status', 'queued')
    if (cancelError) {
      setError(cancelError.message)
      return
    }
    setCloudAuthMessage('已取消排队中的云端任务。')
    await refreshCloudTasks()
  }

  const retryCloudTask = async (task: CloudTask) => {
    if (!supabase || !cloudUser) return
    const projectId = task.project_id ?? cloudProjectId
    if (!projectId) {
      setError('这个失败任务缺少项目 ID，无法安全重试。请重新提交当前项目任务。')
      return
    }
    if (!task.payload) {
      setError('这个失败任务缺少原始参数，无法自动重试。请重新提交当前项目任务。')
      return
    }
    if (cloudSubmitLock.current) {
      setError('任务正在提交，请稍等。')
      return
    }
    cloudSubmitLock.current = true
    setCloudSubmitting(true)
    setError('')
    try {
      const { data: activeTasks, error: activeError } = await supabase
        .from('streetscope_tasks')
        .select('id')
        .eq('project_id', projectId)
        .eq('user_id', cloudUser.id)
        .eq('kind', task.kind)
        .in('status', ['queued', 'running'])
        .limit(1)
      if (activeError) throw activeError
      if (activeTasks?.length) {
        setCloudAuthMessage('当前项目已有同类任务在排队或运行，不需要重复重试。')
        await refreshCloudTasks(projectId)
        return
      }
      const { error: retryError } = await supabase.from('streetscope_tasks').insert({
        user_id: cloudUser.id,
        project_id: projectId,
        kind: task.kind,
        status: 'queued',
        payload: task.payload,
        message: `重试：${readableCloudTaskKind(task.kind)}`,
      })
      if (retryError) throw retryError
      setCloudProjectId(projectId)
      setCloudAuthMessage('已重新提交失败任务，NAS Worker 会继续执行。')
      await refreshCloudTasks(projectId)
    } catch (err) {
      setError(err instanceof Error ? err.message : '云端任务重试失败')
    } finally {
      cloudSubmitLock.current = false
      setCloudSubmitting(false)
    }
  }

  const startUploadedImageMetrics = async (files: FileList | null) => {
    if (!files?.length) return
    if (!localBackendEnabled) {
      setError('公网模式暂不支持直接上传图片到 Vercel 分割；请在本地模式使用该功能，或提交云端完整任务。')
      return
    }
    if (!segmentationServiceUrl.trim()) {
      setError('上传图片真实分割需要先填写模型服务地址。')
      return
    }
    setError('')
    try {
      const formData = new FormData()
      Array.from(files).forEach((file) => formData.append('files', file))
      formData.append('project_name', projectName)
      formData.append('model_name', modelName)
      formData.append('selected_metrics', selectedMetrics.join(','))
      formData.append('inference_mode', inferenceMode)
      formData.append('segmentation_service_url', segmentationServiceUrl)
      const { task_id } = await uploadApi<{ task_id: string; image_count: string }>('/api/uploaded-image-metrics-task', formData)
      setMetricsTask({
        task_id,
        kind: 'metrics',
        status: 'queued',
        progress: 0,
        total: 0,
        succeeded: 0,
        failed: 0,
        message: '上传图片分割任务已提交',
        records: [],
      })
      setActiveStep(3)
      refreshOverview()
    } catch (err) {
      setError(readableApiError(err, '上传图片分割任务创建失败'))
    }
  }

  const setCenterBox = (lat: number, lng: number) => {
    if (!boundaryVisible || drawingBoundary) return
    const latSpan = Math.max(boundary.north - boundary.south, 0.035)
    const lngSpan = Math.max(boundary.east - boundary.west, 0.045)
    setBoundary({
      north: Number((lat + latSpan / 2).toFixed(6)),
      south: Number((lat - latSpan / 2).toFixed(6)),
      east: Number((lng + lngSpan / 2).toFixed(6)),
      west: Number((lng - lngSpan / 2).toFixed(6)),
    })
  }

  const toggleHeading = (heading: number) => {
    setHeadings((current) =>
      current.includes(heading) ? current.filter((item) => item !== heading) : [...current, heading].sort((a, b) => a - b),
    )
  }

  const importBoundaryFile = async (file: File | null) => {
    if (!file) return
    if (!localBackendEnabled) {
      setError('公网模式暂不支持导入边界文件；请先用地图绘制研究区，或在本地模式导入文件。')
      return
    }
    setError('')
    try {
      const formData = new FormData()
      formData.append('file', file)
      const result = await uploadApi<Boundary>('/api/import-boundary', formData)
      setBoundary(result)
      setBoundaryVisible(true)
      setDrawingBoundary(false)
      setFitBoundaryVersion((value) => value + 1)
      setSample(null)
      setActiveStep(0)
    } catch (err) {
      setError(readableApiError(err, '边界文件解析失败'))
    }
  }

  const importSamplePointFile = async (file: File | null) => {
    if (!file) return
    if (!localBackendEnabled) {
      setError('公网模式暂不支持导入采样点文件；请先加载 OSM 路网生成采样点，或在本地模式导入文件。')
      return
    }
    setError('')
    setSampling(true)
    try {
      const formData = new FormData()
      formData.append('file', file)
      const result = await uploadApi<SampleResponse>('/api/import-points?coord_type=wgs84', formData)
      setSample(result)
      if (result.points.length) {
        const lngs = result.points.map((point) => point.lng)
        const lats = result.points.map((point) => point.lat)
        setBoundary({
          west: Number(Math.min(...lngs).toFixed(6)),
          east: Number(Math.max(...lngs).toFixed(6)),
          south: Number(Math.min(...lats).toFixed(6)),
          north: Number(Math.max(...lats).toFixed(6)),
        })
        setBoundaryVisible(true)
        setDrawingBoundary(false)
        setFitBoundaryVersion((value) => value + 1)
      }
      setActiveStep(1)
    } catch (err) {
      setError(readableApiError(err, '采样点文件解析失败'))
    } finally {
      setSampling(false)
    }
  }

  const importRoadGeoJson = async (file: File | null) => {
    if (!file) return
    if (!localBackendEnabled) {
      setError('公网模式暂不支持导入路网文件；请先使用“加载 OSM 路网”，或在本地模式导入文件。')
      return
    }
    setError('')
    setSampling(true)
    try {
      const formData = new FormData()
      formData.append('file', file)
      const result = await uploadApi<SampleResponse>(`/api/import-roads?interval_m=${intervalM}&clean_roads=${cleanRoads}`, formData)
      setSample(result)
      if (result.points.length) {
        const lngs = result.points.map((point) => point.lng)
        const lats = result.points.map((point) => point.lat)
        setBoundary({
          west: Number(Math.min(...lngs).toFixed(6)),
          east: Number(Math.max(...lngs).toFixed(6)),
          south: Number(Math.min(...lats).toFixed(6)),
          north: Number(Math.max(...lats).toFixed(6)),
        })
        setBoundaryVisible(true)
        setDrawingBoundary(false)
        setFitBoundaryVersion((value) => value + 1)
      }
      setActiveStep(1)
    } catch (err) {
      setError(readableApiError(err, '路网文件解析失败'))
    } finally {
      setSampling(false)
    }
  }

  const startNewProject = () => {
    setProjectName('未命名街景研究项目')
    setBoundary(adminPresets.徐汇示范区)
    setBoundaryVisible(false)
    setDrawingBoundary(false)
    setFitBoundaryVersion(0)
    setIntervalM(100)
    setRoadDensity('medium')
    setOsmWalkableOnly(true)
    setOsmExcludeHighSpeed(true)
    setCleanRoads(true)
    setSample(null)
    setDownloadProvider('baidu_web')
    setUseRealBaidu(false)
    setHeadings([0, 90, 180, 270])
    setImageMode('directions')
    setSkipExisting(true)
    setConcurrency(2)
    setRetryCount(1)
    setImageWidth(1024)
    setImageHeight(512)
    setPitch(0)
    setFov(90)
    setCoordtype('bd09ll')
    setDownloadTask(null)
    setMetricsTask(null)
    setCloudProjectId(null)
    setCloudTasks([])
    setModelName('Mask2Former + ADE20K')
    setSelectedMetrics(defaultSelectedMetrics)
    setSegmentationServiceUrl('')
    setError('')
    setActiveStep(0)
    setView('workspace')
  }

  const openRecentProject = (project: ProjectSummary) => {
    setProjectName(project.project_name)
    setDownloadTask(null)
    setMetricsTask(null)
    setCloudProjectId(null)
    setCloudTasks([])
    setError('')
    setActiveStep(4)
    setView('workspace')
  }

  const importProjectConfig = async (file: File | null) => {
    if (!file) return
    setError('')
    try {
      const parsed = JSON.parse(await file.text()) as ProjectConfig
      applyProjectConfig(parsed)
      setDraftStatus('已导入项目配置')
      setView('workspace')
    } catch (err) {
      setError(err instanceof Error ? err.message : '项目配置导入失败')
    }
  }

  const estimateDirections = imageMode === 'panorama' ? 0 : headings.length
  const estimatePanorama = imageMode === 'panorama' ? 1 : 0
  const estimateImages = (sample?.points.length ?? 0) * (estimateDirections + estimatePanorama)
  const finishedDownload = downloadTask?.status === 'completed'
  const finishedMetrics = metricsTask?.status === 'completed'
  const projectCloudTasks = cloudProjectId ? cloudTasks.filter((task) => task.project_id === cloudProjectId) : []
  const currentCloudRun = projectCloudTasks.find((task) => task.kind === 'download_then_metrics') ?? null
  const currentCloudDownload = projectCloudTasks.find((task) => task.kind === 'download') ?? null
  const completedCloudDownload = projectCloudTasks.find((task) => task.kind === 'download' && artifactPaths(task).length > 0) ?? null
  const cloudDeliveryTask =
    projectCloudTasks.find((task) => task.kind === 'download_then_metrics' && artifactPaths(task).length > 0) ??
    projectCloudTasks.find((task) => (task.kind === 'metrics' || task.kind === 'uploaded_metrics') && artifactPaths(task).length > 0) ??
    null
  const cloudDeliveryArtifacts = cloudDeliveryTask ? artifactPaths(cloudDeliveryTask) : []
  const hasRunningCloudTask = projectCloudTasks.some(isCloudTaskActive)
  const cloudDownloadReady = Boolean(
    cloudDeliveryTask ||
      currentCloudDownload?.status === 'completed' ||
      (currentCloudRun && (currentCloudRun.local_download_task_id || currentCloudRun.progress > 0)),
  )
  const cloudMetricsReady = Boolean(cloudDeliveryTask)
  const deliveryReady = Boolean(cloudDeliveryTask || (finishedMetrics && metricsTask))
  const stepGuides = [
    {
      title: '1. 选择研究区',
      description: '先用行政区预设、上传边界，或在地图上拖拽绘制矩形范围。矩形生成后可拖中心点移动，也可拖边角调整。',
    },
    {
      title: '2. 清洗路网并采样',
      description: '设置采样间隔和路网过滤规则，生成可在 GIS 中继续使用的采样点、路网和边界图层。',
    },
    {
      title: '3. 下载街景图像',
      description: '选择官方 API 或授权 Web 无 AK，配置图像方向、尺寸、并发和本地归档复用策略。',
    },
    {
      title: '4. 计算语义指标',
      description: '选择语义分割模型和指标，也可以上传已有图片或 ZIP，输出绿视率、天空开阔度等论文常用变量。',
    },
    {
      title: '5. 导出与复核',
      description: '检查任务结果、质量标记和数据包结构，最后导出一份完整论文数据包。',
    },
  ]
  const currentGuide = stepGuides[activeStep] ?? stepGuides[0]
  const selectedModelParts = modelParts(modelName)
  const selectedModelDeployed = deployedModelNames.has(modelName)
  const nextDisabled =
    (activeStep === 0 && !boundaryVisible) ||
    (activeStep === 1 && !sample?.points.length) ||
    (activeStep === 2 && !sample?.points.length) ||
    (activeStep === 3 && !selectedMetrics.length)
  const canVisitStep = (index: number) => {
    if (index === 0) return true
    if (index === 1) return boundaryVisible || Boolean(sample?.points.length)
    if (index === 2 || index === 3) return Boolean(sample?.points.length)
    return Boolean(sample?.points.length || downloadTask || metricsTask || cloudProjectId || projectCloudTasks.length)
  }
  const goPrevStep = () => setActiveStep((step) => Math.max(0, step - 1))
  const goNextStep = () => setActiveStep((step) => Math.min(workflowSteps.length - 1, step + 1))

  if (view === 'home') {
    return (
      <main className="home-shell">
        <section className="home-rail" aria-label="项目入口">
          <div className="brand">
            <span className="brand-mark">
              <MapPinned size={22} aria-hidden="true" />
            </span>
            <div>
              <h1>StreetScope</h1>
              <p>街景研究数据生成器</p>
            </div>
          </div>
          <nav className="home-nav" aria-label="主页导航">
            <button type="button" className="active">
              <Home size={17} aria-hidden="true" />
              主页
            </button>
            <button type="button" onClick={startNewProject}>
              <Plus size={17} aria-hidden="true" />
              新建项目
            </button>
            <label>
              <FileUp size={17} aria-hidden="true" />
              导入项目
              <input type="file" accept=".json,application/json" onChange={(event) => importProjectConfig(event.target.files?.[0] ?? null)} />
            </label>
          </nav>
          <CloudAccountCard
            cloudUser={cloudUser}
            cloudEmail={cloudEmail}
            cloudPassword={cloudPassword}
            cloudAuthMessage={cloudAuthMessage}
            configured={isSupabaseConfigured}
            onEmailChange={setCloudEmail}
            onPasswordChange={setCloudPassword}
            onSignIn={signInCloud}
            onSignUp={signUpCloud}
            onSignOut={signOutCloud}
          />
        </section>

        <section className="home-main">
          <header className="home-hero">
            <div>
              <p className="eyebrow">Project Home</p>
              <h2>打开一个街景研究项目</h2>
              <p>像打开 PSD 一样打开研究任务：继续草稿、新建项目，或从最近导出的任务回到复核。</p>
            </div>
            <button type="button" className="primary-action" onClick={startNewProject}>
              <Plus size={18} aria-hidden="true" />
              新建采集项目
            </button>
          </header>

          <section className="home-grid" aria-label="项目文件">
            <button type="button" className="project-tile continue-tile" onClick={() => setView('workspace')}>
              <div className="tile-icon">
                <FolderOpen size={24} aria-hidden="true" />
              </div>
              <div>
                <strong>{projectName}</strong>
                <span>{sample ? `${formatNumber(sample.points.length)} 个采样点 · ${formatNumber(estimateImages)} 张预计图像` : '继续本地自动保存草稿'}</span>
                <em>{draftStatus}</em>
              </div>
            </button>

            <label className="project-tile import-tile">
              <div className="tile-icon">
                <FileUp size={24} aria-hidden="true" />
              </div>
              <div>
                <strong>导入项目文件</strong>
                <span>打开之前导出的 StreetScope JSON 配置</span>
                <em>API Key 不会保存在项目文件中</em>
              </div>
              <input type="file" accept=".json,application/json" onChange={(event) => importProjectConfig(event.target.files?.[0] ?? null)} />
            </label>
          </section>

          <section className="home-section">
            <div className="home-section-title">
              <h3>最近项目</h3>
              <button type="button" className="mini-action" onClick={refreshOverview}>
                刷新
              </button>
            </div>
            <div className="recent-project-grid">
              {recentProjects.slice(0, 8).map((project) => (
                <button type="button" className="recent-project-card" key={project.project_name} onClick={() => openRecentProject(project)}>
                  <span>{project.project_name}</span>
                  <strong>{project.task_count} 个任务 · 完成 {project.completed_count}</strong>
                  <em>{project.latest_status || '暂无状态'}</em>
                </button>
              ))}
              {!recentProjects.length ? (
                <div className="empty-home">
                  <Archive size={22} aria-hidden="true" />
                  <p>暂无最近项目。新建一个项目后，采集和导出的任务会出现在这里。</p>
                </div>
              ) : null}
            </div>
          </section>
        </section>
      </main>
    )
  }

  return (
    <main className="app-shell wizard-shell">
      <aside className="sidebar" aria-label="项目配置">
        <div className="brand">
          <span className="brand-mark">
            <MapPinned size={22} aria-hidden="true" />
          </span>
          <div>
            <h1>StreetScope</h1>
            <p>街景研究数据生成器</p>
          </div>
        </div>

        <label className="field">
          <span>项目名称</span>
          <input value={projectName} onChange={(event) => setProjectName(event.target.value)} />
        </label>
        <div className="project-actions">
          <label className="file-action compact">
            <FileUp size={15} aria-hidden="true" />
            导入项目
            <input type="file" accept=".json,application/json" onChange={(event) => importProjectConfig(event.target.files?.[0] ?? null)} />
          </label>
        </div>
        <p className="draft-status">{draftStatus}，API Key 不会写入项目文件。</p>
        <CloudAccountCard
          cloudUser={cloudUser}
          cloudEmail={cloudEmail}
          cloudPassword={cloudPassword}
          cloudAuthMessage={cloudAuthMessage}
          configured={isSupabaseConfigured}
          compact
          onEmailChange={setCloudEmail}
          onPasswordChange={setCloudPassword}
          onSignIn={signInCloud}
          onSignUp={signUpCloud}
          onSignOut={signOutCloud}
        />
        <section className="cloud-card service-card">
          <div className="cloud-title">
            <Route size={18} aria-hidden="true" />
            <h2>NAS 数据服务</h2>
          </div>
          <label className="field compact-field">
            <span>服务地址</span>
            <input
              value={localApiBase}
              onChange={(event) => {
                setLocalApiBase(event.target.value)
                setLocalApiStatus('unknown')
                setLocalApiMessage('地址已修改，请检测连接')
              }}
              placeholder="http://NAS-IP:8000"
            />
          </label>
          <div className="service-status-row">
            <span className={`service-dot ${localApiStatus}`} aria-hidden="true" />
            <em>{localApiMessage || '用于加载路网、导入 GIS 文件和本地质检'}</em>
          </div>
          <button type="button" className="mini-action full" onClick={() => checkLocalApi()} disabled={localApiStatus === 'checking'}>
            {localApiStatus === 'checking' ? <Loader2 className="spin" size={16} aria-hidden="true" /> : <Activity size={16} aria-hidden="true" />}
            检测连接
          </button>
        </section>

        <section className="step-guide" aria-label="当前步骤">
          <div>
            <span className="step-count">步骤 {activeStep + 1} / {workflowSteps.length}</span>
            <h2>{currentGuide.title}</h2>
            <p>{currentGuide.description}</p>
          </div>
          <div className="wizard-actions">
            <button type="button" className="mini-action" onClick={goPrevStep} disabled={activeStep === 0}>
              上一步
            </button>
            <button type="button" className="primary-action" onClick={goNextStep} disabled={activeStep === workflowSteps.length - 1 || nextDisabled}>
              下一步
            </button>
          </div>
        </section>

        {activeStep === 0 ? (
        <section className="panel">
          <div className="panel-title">
            <Layers size={18} aria-hidden="true" />
            <h2>研究区</h2>
          </div>
          <div className="preset-grid">
            {Object.keys(adminPresets).map((name) => (
              <button
                key={name}
                type="button"
                className="chip"
                onClick={() => {
                  setBoundary(adminPresets[name])
                  setBoundaryVisible(true)
                  setDrawingBoundary(false)
                  setFitBoundaryVersion((value) => value + 1)
                  setSample(null)
                  setActiveStep(0)
                }}
              >
                {name}
              </button>
            ))}
          </div>
          <div className="export-row">
            {!boundaryVisible ? (
              <button
                type="button"
                className="mini-action"
                onClick={() => {
                  setBoundaryVisible(true)
                  setDrawingBoundary(false)
                  setFitBoundaryVersion((value) => value + 1)
                }}
              >
                显示当前边界
              </button>
            ) : null}
            <button
              type="button"
              className={drawingBoundary ? 'mini-action active' : 'mini-action'}
              onClick={() => {
                setDrawingBoundary(true)
                setBoundaryVisible(false)
                setSample(null)
                setActiveStep(0)
              }}
            >
              绘制矩形研究区
            </button>
            <button
              type="button"
              className="mini-action"
              onClick={() => {
                setDrawingBoundary(false)
                setBoundaryVisible(false)
                setSample(null)
                setActiveStep(0)
              }}
            >
              清除边界
            </button>
          </div>
          <div className="coordinate-grid">
            {(['north', 'south', 'east', 'west'] as const).map((key) => (
              <label key={key} className="mini-field">
                <span>{key}</span>
                <input
                  type="number"
                  step="0.0001"
                  disabled={!boundaryVisible}
                  value={boundary[key]}
                  onChange={(event) => {
                    setBoundaryVisible(true)
                    setBoundary({ ...boundary, [key]: Number(event.target.value) })
                  }}
                />
              </label>
            ))}
          </div>
          <label className="file-action">
            <FileDown size={16} aria-hidden="true" />
            上传边界文件
            <input
              type="file"
              accept=".geojson,.json,.kml,.zip,application/geo+json,application/json,application/vnd.google-earth.kml+xml,application/zip"
              onChange={(event) => importBoundaryFile(event.target.files?.[0] ?? null)}
            />
          </label>
        </section>
        ) : null}

        {activeStep === 1 ? (
        <section className="panel">
          <div className="panel-title">
            <Route size={18} aria-hidden="true" />
            <h2>采样设置</h2>
          </div>
          <label className="field">
            <span>采样间隔 {intervalM}m</span>
            <input type="range" min="25" max="500" step="25" value={intervalM} onChange={(event) => setIntervalM(Number(event.target.value))} />
          </label>
          <label className="mini-field">
            <span>自定义间隔 m</span>
            <input
              type="number"
              min="25"
              max="500"
              step="5"
              value={intervalM}
              onChange={(event) => {
                const next = Number(event.target.value)
                if (Number.isFinite(next)) setIntervalM(Math.min(500, Math.max(25, next)))
              }}
            />
          </label>
          <p className="inline-note">生产模式不生成内置网格道路。路网加载和文件导入需要 NAS 数据服务在线。</p>
          <div className="toggle-grid">
            <label className="toggle">
              <input type="checkbox" checked={osmWalkableOnly} onChange={(event) => setOsmWalkableOnly(event.target.checked)} />
              <span>仅步行/城市道路</span>
            </label>
            <label className="toggle">
              <input type="checkbox" checked={osmExcludeHighSpeed} onChange={(event) => setOsmExcludeHighSpeed(event.target.checked)} />
              <span>排除高速快速路</span>
            </label>
            <label className="toggle">
              <input type="checkbox" checked={cleanRoads} onChange={(event) => setCleanRoads(event.target.checked)} />
              <span>清洗路网</span>
            </label>
          </div>
          <button type="button" className="secondary-action" onClick={loadOsmRoads} disabled={sampling || !localBackendEnabled}>
            {sampling ? <Loader2 className="spin" size={18} aria-hidden="true" /> : <Route size={18} aria-hidden="true" />}
            加载 OSM 路网
          </button>
          <div className="upload-grid">
            <label className="file-action compact">
              <FileUp size={16} aria-hidden="true" />
              导入点 CSV/SHP
              <input type="file" accept=".csv,.zip,text/csv,application/zip" onChange={(event) => importSamplePointFile(event.target.files?.[0] ?? null)} />
            </label>
            <label className="file-action compact">
              <FileUp size={16} aria-hidden="true" />
              导入路网
              <input type="file" accept=".geojson,.json,.zip,application/geo+json,application/json,application/zip" onChange={(event) => importRoadGeoJson(event.target.files?.[0] ?? null)} />
            </label>
          </div>
        </section>
        ) : null}

        {activeStep === 2 ? (
        <section className="panel">
          <div className="panel-title">
            <KeyRound size={18} aria-hidden="true" />
            <h2>百度街景</h2>
          </div>
          <label className="field">
            <span>下载来源</span>
            <select
              value={downloadProvider}
              onChange={(event) => {
                const next = event.target.value as 'baidu' | 'baidu_web'
                setDownloadProvider(next)
                setUseRealBaidu(next === 'baidu')
              }}
            >
              <option value="baidu_web">授权 Web 无 AK</option>
              <option value="baidu">官方 API Key</option>
            </select>
          </label>
          {downloadProvider === 'baidu' ? (
            <>
              <label className="field">
                <span>API Key</span>
                <input value={ak} onChange={(event) => setAk(event.target.value)} placeholder="填写百度开放平台 AK" />
              </label>
              <button type="button" className="mini-action full" onClick={testBaiduKey} disabled={!ak.trim() || testingKey}>
                {testingKey ? <Loader2 className="spin" size={16} aria-hidden="true" /> : <KeyRound size={16} aria-hidden="true" />}
                测试 Key
              </button>
              {keyTestMessage ? <p className="inline-note">{keyTestMessage}</p> : null}
            </>
          ) : null}
          {downloadProvider === 'baidu_web' ? <p className="inline-note">已启用授权 Web 无 AK 模式：四方向使用 pr3d 视角图；原生全景使用 pdata 瓦片自动拼接。</p> : null}
          <label className="field">
            <span>图像类型</span>
            <select value={imageMode} onChange={(event) => setImageMode(event.target.value as 'directions' | 'stitched' | 'panorama')}>
              <option value="directions">四方向图（不拼接，单独计算）</option>
              <option value="stitched">四方向图（拼接后计算）</option>
              <option value="panorama">全景图</option>
            </select>
          </label>
          <div className="coordinate-grid">
            <label className="mini-field">
              <span>宽度</span>
              <input type="number" min="256" max="2048" step="128" value={imageWidth} onChange={(event) => setImageWidth(Number(event.target.value))} />
            </label>
            <label className="mini-field">
              <span>高度</span>
              <input type="number" min="256" max="2048" step="128" value={imageHeight} onChange={(event) => setImageHeight(Number(event.target.value))} />
            </label>
            <label className="mini-field">
              <span>pitch</span>
              <input type="number" min="0" max="90" value={pitch} onChange={(event) => setPitch(Number(event.target.value))} />
            </label>
            <label className="mini-field">
              <span>fov</span>
              <input type="number" min="10" max="360" value={fov} onChange={(event) => setFov(Number(event.target.value))} />
            </label>
            <label className="mini-field">
              <span>并发数</span>
              <input type="number" min="1" max="8" value={concurrency} onChange={(event) => setConcurrency(Math.min(8, Math.max(1, Number(event.target.value))))} />
            </label>
            <label className="mini-field">
              <span>失败重试</span>
              <input type="number" min="0" max="5" value={retryCount} onChange={(event) => setRetryCount(Math.min(5, Math.max(0, Number(event.target.value))))} />
            </label>
          </div>
          {downloadProvider === 'baidu_web' && imageMode === 'panorama' ? <p className="inline-note">全景图使用原生全景瓦片，输出约 4096×2048；语义分割前会转为水平视角图以避开采集车底部。</p> : null}
          {imageMode === 'directions' ? <p className="inline-note">四方向图会按 0/90/180/270 分别分割并汇总指标；横向拼图仅作为质检预览，不作为模型输入。</p> : null}
          {imageMode === 'stitched' ? <p className="inline-note">四方向图会先横向拼接为一张图，再提交模型计算；适合复刻部分商家交付口径。</p> : null}
          <label className="toggle">
            <input type="checkbox" checked={skipExisting} onChange={(event) => setSkipExisting(event.target.checked)} />
            <span>跳过已下载图片</span>
          </label>
          <p className="inline-note">坐标系自动处理：采样点和 GIS 导出保留 WGS84；请求百度时自动使用 BD09 / 百度墨卡托。</p>
          <div className="heading-row" aria-label="街景方向">
            {[0, 90, 180, 270].map((heading) => (
              <button
                key={heading}
                type="button"
                className={headings.includes(heading) ? 'selected' : ''}
                onClick={() => toggleHeading(heading)}
                disabled={imageMode === 'panorama'}
              >
                {heading}°
              </button>
            ))}
          </div>
          <button
            type="button"
            className="secondary-action"
            onClick={startDownload}
            disabled={!sample?.points.length || (imageMode !== 'panorama' && !headings.length) || (downloadProvider === 'baidu' && !ak.trim()) || (useCloudQueue && (cloudSubmitting || hasRunningCloudTask))}
          >
            {useCloudQueue && cloudSubmitting ? <Loader2 className="spin" size={18} aria-hidden="true" /> : <Download size={18} aria-hidden="true" />}
            {useCloudQueue ? (cloudSubmitting ? '正在提交云端任务' : hasRunningCloudTask ? '云端任务执行中' : '提交云端下载任务') : '创建本地抓图任务'}
          </button>
          {useCloudQueue ? <p className="inline-note">公网登录后优先提交 Supabase 队列，由 NAS Worker 执行并上传 ZIP，避免重复任务和浏览器长时间占用。</p> : null}
        </section>
        ) : null}

        {activeStep === 3 ? (
        <section className="panel">
          <div className="panel-title">
            <Sparkles size={18} aria-hidden="true" />
            <h2>语义分割</h2>
          </div>
          <div className="coordinate-grid">
            <label className="mini-field">
              <span>模型架构</span>
              <select
                value={selectedModelParts.architecture}
                onChange={(event) => setModelName(`${event.target.value} + ${selectedModelParts.dataset}`)}
              >
                {modelArchitectures.map((architecture) => <option key={architecture}>{architecture}</option>)}
              </select>
            </label>
            <label className="mini-field">
              <span>训练数据集</span>
              <select
                value={selectedModelParts.dataset}
                onChange={(event) => setModelName(`${selectedModelParts.architecture} + ${event.target.value}`)}
              >
                {modelDatasets.map((dataset) => <option key={dataset}>{dataset}</option>)}
              </select>
            </label>
          </div>
          <p className="inline-note">
            当前模型：{modelName}。已提前缓存；Mask2Former 由主服务推理，FCN/PSPNet/DeepLabv3 由 MMSegmentation sidecar 推理。
          </p>
          <p className="inline-note">生产模式只使用真实分割：系统会把已下载的街景图片提交给你的模型服务，返回类别占比或标准化指标。</p>
          <label className="field">
            <span>模型服务地址</span>
            <input value={segmentationServiceUrl} onChange={(event) => setSegmentationServiceUrl(event.target.value)} placeholder="GPU 开机后填写：http://GPU服务器IP:9000/segment" />
          </label>
          <div className="check-grid" aria-label="指标选择">
            {metricOptions.map((metric) => (
              <label key={metric.key}>
                <input
                  type="checkbox"
                  checked={selectedMetrics.includes(metric.key)}
                  onChange={(event) => {
                    setSelectedMetrics((current) =>
                      event.target.checked ? [...current, metric.key] : current.filter((key) => key !== metric.key),
                    )
                  }}
                />
                <span>{metric.label}</span>
              </label>
            ))}
          </div>
          <button
            type="button"
            className="secondary-action"
            onClick={startMetrics}
            disabled={!sample?.points.length || !selectedMetrics.length || !segmentationServiceUrl.trim() || !finishedDownload || !selectedModelDeployed}
          >
            <Activity size={18} aria-hidden="true" />
            用已下载图片计算指标
          </button>
          <button
            type="button"
            className="secondary-action"
            onClick={submitCloudFullTask}
            disabled={!cloudReady || cloudSubmitting || hasRunningCloudTask || !sample?.points.length || !selectedMetrics.length || !segmentationServiceUrl.trim() || !selectedModelDeployed || (downloadProvider === 'baidu' && !ak.trim())}
          >
            {cloudSubmitting ? <Loader2 className="spin" size={18} aria-hidden="true" /> : <Cloud size={18} aria-hidden="true" />}
            {hasRunningCloudTask ? '云端任务执行中' : '提交云端完整任务'}
          </button>
          <p className="inline-note">公网使用推荐点“提交云端完整任务”：任务会进入 Supabase 队列，Windows Worker 负责下载、分割并上传最终 ZIP。</p>
          <label className="file-action">
            <FileUp size={16} aria-hidden="true" />
            上传图片/ZIP 分割
            <input
              type="file"
              multiple
              accept=".jpg,.jpeg,.png,.webp,.zip,image/jpeg,image/png,image/webp,application/zip"
              onChange={(event) => startUploadedImageMetrics(event.target.files)}
            />
          </label>
        </section>
        ) : null}

        {activeStep === 4 ? (
          <section className="panel">
            <div className="panel-title">
              <Archive size={18} aria-hidden="true" />
              <h2>导出与复核</h2>
            </div>
            <ul className="review-list">
              <li className={boundaryVisible ? 'ready' : ''}>研究区边界：{boundaryVisible ? '已设置' : '未设置'}</li>
              <li className={sample?.points.length ? 'ready' : ''}>采样点：{sample ? `${formatNumber(sample.points.length)} 个` : '未生成'}</li>
              <li className={finishedDownload || cloudDownloadReady ? 'ready' : ''}>
                街景图像：{cloudDeliveryTask ? '已包含在最终包' : completedCloudDownload ? '已下载，可继续做语义分割' : currentCloudDownload ? `${currentCloudDownload.succeeded}/${currentCloudDownload.total || '-'}` : downloadTask ? `${downloadTask.succeeded}/${downloadTask.total}` : '未创建任务'}
              </li>
              <li className={finishedMetrics || cloudMetricsReady ? 'ready' : ''}>
                语义指标：{cloudDeliveryTask ? '已生成' : currentCloudRun ? `${currentCloudRun.succeeded}/${currentCloudRun.total || '-'}` : metricsTask ? `${metricsTask.succeeded}/${metricsTask.total}` : completedCloudDownload ? '未生成，请提交完整生产任务' : '未创建任务'}
              </li>
            </ul>
            {cloudDeliveryTask && cloudDeliveryArtifacts.length ? (
              <div className="export-stack">
                {cloudDeliveryArtifacts.map((artifactPath, index) => (
                  <button type="button" className="export-link" key={artifactPath} onClick={() => downloadCloudArtifact(cloudDeliveryTask, artifactPath)}>
                    <FileDown size={16} aria-hidden="true" />
                    {cloudDeliveryArtifacts.length > 1 ? `下载论文数据包 ZIP ${index + 1}` : '下载论文数据包 ZIP'}
                  </button>
                ))}
              </div>
            ) : finishedMetrics && metricsTask ? (
              <a className="export-link" href={`${backendBase}/api/export/${metricsTask.task_id}`}>
                <FileDown size={16} aria-hidden="true" />
                导出论文数据包 ZIP
              </a>
            ) : (
              <button type="button" className="export-link" disabled>
                <FileDown size={16} aria-hidden="true" />
                导出论文数据包 ZIP
              </button>
            )}
            <p className="inline-note">包含研究区、路网、采样点、街景图像、语义分割结果、指标 CSV 与 GIS 文件。</p>
          </section>
        ) : null}
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <p className="eyebrow">PRD MVP Workflow</p>
            <h2>从研究区到论文数据包</h2>
          </div>
          <button type="button" className="home-return" onClick={() => setView('home')}>
            <Home size={16} aria-hidden="true" />
            主页
          </button>
          <div className="stepper" aria-label="工作流进度">
            {workflowSteps.map((step, index) => (
              <button
                key={step}
                type="button"
                className={`${index <= activeStep ? 'done' : ''} ${index === activeStep ? 'active-step' : ''}`.trim()}
                onClick={() => setActiveStep(index)}
                disabled={!canVisitStep(index)}
              >
                {index < activeStep ? <CheckCircle2 size={15} aria-hidden="true" /> : index + 1}
                {step}
              </button>
            ))}
          </div>
        </header>

        {error ? <div className="error-box" role="alert">{error}</div> : null}

        <section className="notice-box">
          <KeyRound size={17} aria-hidden="true" />
          <span>百度 API Key 仅用于当前请求，不写入项目 JSON；真实下载请确认调用额度、费用和地图平台数据使用条款。</span>
        </section>

        {activeStep === 4 ? (
        <section className="overview-grid" aria-label="项目中心">
          <div className="surface delivery-hero">
            <div className="surface-title">
              <Archive size={18} aria-hidden="true" />
              <h3>交付状态</h3>
            </div>
            <div className="project-summary">
              <strong>{projectName}</strong>
              <span>{sample ? `${formatNumber(sample.points.length)} 个采样点 · ${formatNumber(estimateImages)} 张预计图像` : '尚未生成采样点'}</span>
              <span className={deliveryReady ? 'status-good' : hasRunningCloudTask ? 'status-warn' : completedCloudDownload ? 'status-warn' : 'status-muted'}>
                {deliveryReady
                  ? '最终论文数据包已就绪'
                  : hasRunningCloudTask
                    ? '生产任务执行中，请等待 NAS Worker 完成'
                    : completedCloudDownload
                      ? '街景图像已下载，语义指标还未生成'
                      : '还没有可交付的最终数据包'}
              </span>
            </div>
          </div>

          <div className="surface delivery-actions">
            <div className="surface-title">
              <FileDown size={18} aria-hidden="true" />
              <h3>最终下载</h3>
            </div>
            {cloudDeliveryTask && cloudDeliveryArtifacts.length ? (
              <div className="export-stack">
                {cloudDeliveryArtifacts.map((artifactPath, index) => (
                  <button type="button" className="export-link" key={artifactPath} onClick={() => downloadCloudArtifact(cloudDeliveryTask, artifactPath)}>
                    <FileDown size={16} aria-hidden="true" />
                    {cloudDeliveryArtifacts.length > 1 ? `ZIP ${index + 1}` : '下载 ZIP'}
                  </button>
                ))}
              </div>
            ) : finishedMetrics && metricsTask ? (
              <a className="export-link" href={`${backendBase}/api/export/${metricsTask.task_id}`}>
                <FileDown size={16} aria-hidden="true" />
                下载 ZIP
              </a>
            ) : (
              <button type="button" className="export-link" disabled>
                <FileDown size={16} aria-hidden="true" />
                等待最终 ZIP
              </button>
            )}
            <p className="inline-note">只保留最终交付包入口；中间过程文件会包含在 ZIP 内。</p>
          </div>

          <div className="surface">
            <div className="surface-title">
              <Cloud size={18} aria-hidden="true" />
              <h3>当前云端任务</h3>
              <button type="button" className="mini-action" onClick={() => refreshCloudTasks()} disabled={!cloudReady || !cloudProjectId}>
                刷新
              </button>
            </div>
            <div className="compact-list">
              {projectCloudTasks.map((task) => (
                <div className="compact-row task-row" key={task.id}>
                  <div>
                    <strong>{readableCloudTaskKind(task.kind)}</strong>
                    <span className={task.status === 'failed' ? 'task-error-text' : ''}>{task.status} · {task.progress}% · {readableCloudError(task)}</span>
                  </div>
                  <div className="cloud-task-actions">
                    {task.status === 'queued' ? (
                      <button type="button" className="mini-action" onClick={() => cancelQueuedCloudTask(task)}>
                        取消
                      </button>
                    ) : null}
                    {task.status === 'failed' ? (
                      <button type="button" className="mini-action" onClick={() => retryCloudTask(task)} disabled={cloudSubmitting}>
                        重试
                      </button>
                    ) : null}
                    {artifactPaths(task).length ? (
                      artifactPaths(task).map((artifactPath, index, paths) => (
                        <button type="button" className="mini-action" key={artifactPath} onClick={() => downloadCloudArtifact(task, artifactPath)}>
                          {paths.length > 1 ? `ZIP ${index + 1}` : 'ZIP'}
                        </button>
                      ))
                    ) : (
                      <em>{task.succeeded}/{task.total || '-'}</em>
                    )}
                  </div>
                </div>
              ))}
              {!cloudReady ? <p className="muted">登录云端账号后，当前项目的 NAS Worker 任务会显示在这里。</p> : null}
              {cloudReady && !cloudProjectId ? <p className="muted">当前项目还没有提交过云端任务。</p> : null}
              {cloudReady && cloudProjectId && !projectCloudTasks.length ? <p className="muted">当前项目暂无云端任务。</p> : null}
            </div>
          </div>

          <div className="surface">
            <div className="surface-title">
              <Layers size={18} aria-hidden="true" />
              <h3>数据包结构</h3>
            </div>
            <div className="compact-list">
              <div className="package-line">01_boundary_研究区 / GeoJSON、SHP</div>
              <div className="package-line">02_road_network_路网 / GeoJSON、SHP</div>
              <div className="package-line">03_sampling_points_采样点 / CSV、GeoJSON、SHP</div>
              <div className="package-line">04_streetview_images_街景图像</div>
              <div className="package-line">05_segmentation_语义分割 / 色块图、掩膜图</div>
              <div className="package-line">06_metrics_指标表 / image、point 两级 CSV</div>
            </div>
          </div>
        </section>
        ) : null}

        {activeStep !== 4 ? (
        <section className="map-band">
          <div className="map-wrap">
            <MapContainer center={mapCenter} zoom={12} scrollWheelZoom className="map">
              {boundaryVisible ? <FitBoundary boundary={boundary} version={fitBoundaryVersion} /> : null}
              <TileLayer
                attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
                url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
              />
              {boundaryVisible && !drawingBoundary ? <MapClickSetter onPick={setCenterBox} /> : null}
              <BoundaryDrawTool
                active={drawingBoundary}
                onPreview={(next) => {
                  setBoundary(next)
                  setBoundaryVisible(true)
                }}
                onDone={(next) => {
                  setBoundary(next)
                  setBoundaryVisible(true)
                  setDrawingBoundary(false)
                  setFitBoundaryVersion((value) => value + 1)
                  setSample(null)
                  setActiveStep(0)
                }}
              />
              {boundaryVisible ? (
                <EditableBoundary boundary={boundary} onChange={(next) => {
                  setBoundary(next)
                  setSample(null)
                  setActiveStep(0)
                }} />
              ) : null}
              {sample?.roads.map((road) => (
                <Polyline
                  key={road.road_id}
                  positions={road.coordinates.map(([lng, lat]) => [lat, lng] as LatLngExpression)}
                  pathOptions={{ color: '#2563eb', weight: 2, opacity: 0.65 }}
                />
              ))}
              {metricPreviewPoints.length === 0 && previewPoints.map((point) => (
                <CircleMarker
                  key={point.point_id}
                  center={[point.lat, point.lng]}
                  radius={3}
                  pathOptions={{ color: '#0f766e', fillColor: '#14b8a6', fillOpacity: 0.78, weight: 1 }}
                />
              ))}
              {metricPreviewPoints.map((point) => (
                <CircleMarker
                  key={`metric-${point.point_id}`}
                  center={[point.lat, point.lng]}
                  radius={5}
                  pathOptions={{ color: '#ffffff', fillColor: gviColor(point.gvi), fillOpacity: 0.9, weight: 1.5 }}
                />
              ))}
            </MapContainer>
          </div>
          <div className="map-help">
            <MapPinned size={18} aria-hidden="true" />
            {metricPreviewPoints.length
              ? '当前显示点级 GVI 预览：绿色更高，黄色居中，红色偏低。地图最多预览 260 个指标点，完整结果在导出包中。'
              : drawingBoundary
                ? '在地图上按住鼠标并拖动，松开后生成矩形研究区。生成后可拖动中心点移动，拖动角点或边点调整大小。'
                : boundaryVisible
                  ? '拖动中心点移动矩形；拖动角点或边点调整大小；点击地图也可以移动矩形中心。地图上最多预览 260 个采样点。'
                  : '默认不显示矩形。请点击左侧“绘制矩形研究区”，然后在地图上拖拽画出研究范围。'}
          </div>
        </section>
        ) : null}

        {activeStep > 0 ? (
        <section className="metrics-grid">
          <div className="stat">
            <span>研究区面积</span>
            <strong>{sample ? sample.estimate.area_km2 : '待生成'}{sample ? ' km²' : ''}</strong>
          </div>
          <div className="stat">
            <span>路网长度</span>
            <strong>{sample ? `${sample.estimate.road_length_km} km` : '待生成'}</strong>
          </div>
          <div className="stat">
            <span>清洗路网</span>
            <strong>{sample?.estimate.road_cleaning_enabled ? `${sample.estimate.raw_roads ?? 0}→${sample.estimate.cleaned_roads ?? 0}` : '未启用'}</strong>
          </div>
          <div className="stat">
            <span>采样点</span>
            <strong>{sample ? formatNumber(sample.points.length) : '待生成'}</strong>
          </div>
          <div className="stat">
            <span>预计图像</span>
            <strong>{estimateImages ? formatNumber(estimateImages) : '待生成'}</strong>
          </div>
        </section>
        ) : null}

        <section className="work-grid">
          {activeStep <= 2 ? (
          <div className="surface">
            <div className="surface-title">
              <Settings2 size={18} aria-hidden="true" />
              <h3>任务预估</h3>
            </div>
            <table>
              <tbody>
                <tr>
                  <th>数据来源</th>
                  <td>{sample ? sample.source : '生成采样点后显示'}</td>
                </tr>
                <tr>
                  <th>坐标系</th>
                  <td>WGS84 原始点 + BD09 百度请求点</td>
                </tr>
                <tr>
                  <th>图像参数</th>
                  <td>{imageWidth}×{imageHeight}，pitch {pitch}，fov {fov}，坐标自动转换，{imageMode === 'directions' ? '四方向图（不拼接）' : imageMode === 'stitched' ? '四方向图（拼接后计算）' : '全景图'}，方向 {imageMode === 'panorama' ? 'pano' : headings.join(' / ') || '未选择'}</td>
                </tr>
                <tr>
                  <th>下载策略</th>
                  <td>并发 {concurrency}，失败重试 {retryCount} 次，{skipExisting ? '跳过已下载' : '覆盖重新下载'}</td>
                </tr>
                <tr>
                  <th>API 调用</th>
                  <td>{estimateImages ? `${formatNumber(estimateImages)} 次` : '生成采样点后预估'}</td>
                </tr>
                <tr>
                  <th>导出内容</th>
                  <td>边界、路网、采样点、街景图、mask、指标图层、方法说明</td>
                </tr>
              </tbody>
            </table>
          </div>
          ) : null}

          {(activeStep === 2 || activeStep === 4) ? (
          <TaskPanel
            title="街景图像任务"
            icon={<Image size={18} aria-hidden="true" />}
            task={downloadTask}
            onTaskChange={setDownloadTask}
            request={api}
          />
          ) : null}

          {(activeStep === 3 || activeStep === 4) ? (
          <TaskPanel
            title="语义分割与指标"
            icon={<Sparkles size={18} aria-hidden="true" />}
            task={metricsTask}
            onTaskChange={setMetricsTask}
            request={api}
          />
          ) : null}

          {activeStep === 4 ? (
          <div className="surface">
            <div className="surface-title">
              <Archive size={18} aria-hidden="true" />
              <h3>数据包结构</h3>
            </div>
            <ul className="package-list">
              <li>01_boundary_研究区 / 边界 GeoJSON、SHP</li>
              <li>02_road_network_路网 / 路网 GeoJSON、SHP</li>
              <li>03_sample_points_采样点 / 点位 CSV、GeoJSON、SHP</li>
              <li>04_streetview_images_街景图像 / 原图与质检预览</li>
              <li>05_segmentation_语义分割 / mask、overlay、分割清单</li>
              <li>06_metrics_指标结果 / 指标 CSV、GeoJSON、SHP</li>
              <li>metadata / 参数、字段字典、方法说明</li>
            </ul>
          </div>
          ) : null}
        </section>

        {activeStep === 1 || activeStep === 4 ? (
        <section className="surface full-width">
          <div className="surface-title">
            <FileDown size={18} aria-hidden="true" />
            <h3>采样点预览</h3>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>point_id</th>
                  <th>经度</th>
                  <th>纬度</th>
                  <th>BD09 经度</th>
                  <th>BD09 纬度</th>
                  <th>道路名</th>
                </tr>
              </thead>
              <tbody>
                {(sample?.points.slice(0, 9) ?? []).map((point) => (
                  <tr key={point.point_id}>
                    <td>{point.point_id}</td>
                    <td>{point.lng}</td>
                    <td>{point.lat}</td>
                    <td>{point.lng_bd09}</td>
                    <td>{point.lat_bd09}</td>
                    <td>{point.road_name}</td>
                  </tr>
                ))}
                {!sample ? (
                  <tr>
                    <td colSpan={6}>请先生成采样点。</td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </section>
        ) : null}
      </section>
    </main>
  )
}

function TaskPanel({
  title,
  icon,
  task,
  onTaskChange,
  request,
}: {
  title: string
  icon: React.ReactNode
  task: TaskState | null
  onTaskChange: (task: TaskState) => void
  request: <T>(path: string, options?: RequestInit) => Promise<T>
}) {
  const [busyAction, setBusyAction] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const [qualityFilter, setQualityFilter] = useState('all')
  const [headingFilter, setHeadingFilter] = useState('all')
  const [roadFilter, setRoadFilter] = useState('all')

  const refreshTask = async () => {
    if (!task) return
    const next = await request<TaskState>(`/api/tasks/${task.task_id}`)
    onTaskChange(next)
  }

  const markQuality = async (imageId: string, qualityStatus: 'accepted' | 'low_quality' | 'excluded') => {
    if (!task) return
    setBusyAction(`${imageId}-${qualityStatus}`)
    try {
      await request(`/api/tasks/${task.task_id}/quality`, {
        method: 'POST',
        body: JSON.stringify({ image_id: imageId, quality_status: qualityStatus }),
      })
      await refreshTask()
    } finally {
      setBusyAction('')
    }
  }

  const retryFailed = async () => {
    if (!task) return
    setBusyAction('retry')
    try {
      await request(`/api/tasks/${task.task_id}/retry-failed`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
      await new Promise((resolve) => window.setTimeout(resolve, 500))
      await refreshTask()
    } finally {
      setBusyAction('')
    }
  }

  const controlTask = async (action: 'pause' | 'resume' | 'cancel') => {
    if (!task) return
    setBusyAction(action)
    try {
      const result = await request<{ ok: boolean; task: TaskState }>(`/api/tasks/${task.task_id}/control`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      onTaskChange(result.task)
      if (action === 'resume') {
        await new Promise((resolve) => window.setTimeout(resolve, 500))
        await refreshTask()
      }
    } finally {
      setBusyAction('')
    }
  }

  const records = task?.records ?? []
  const processedCount = task ? task.succeeded + task.failed : 0
  const hasMissingDownloads = Boolean(task && task.kind === 'download' && task.total > 0 && processedCount < task.total)
  const statusOptions = Array.from(new Set(records.map((record) => String(record.status ?? '')).filter(Boolean)))
  const qualityOptions = Array.from(new Set(records.map((record) => String(record.quality_status ?? '')).filter(Boolean)))
  const headingOptions = Array.from(new Set(records.map((record) => String(record.heading ?? '')).filter(Boolean)))
  const roadOptions = Array.from(new Set(records.map((record) => String(record.road_name ?? '')).filter(Boolean))).slice(0, 20)
  const filteredRecords = records.filter((record) => {
    if (statusFilter !== 'all' && String(record.status ?? '') !== statusFilter) return false
    if (qualityFilter !== 'all' && String(record.quality_status ?? '') !== qualityFilter) return false
    if (headingFilter !== 'all' && String(record.heading ?? '') !== headingFilter) return false
    if (roadFilter !== 'all' && String(record.road_name ?? '') !== roadFilter) return false
    return true
  })
  const recentRecords = filteredRecords.slice(0, 8)

  const batchExcludeVisible = async () => {
    if (!task) return
    const imageIds = filteredRecords.map((record) => String(record.image_id ?? '')).filter(Boolean)
    if (!imageIds.length) return
    setBusyAction('batch-exclude')
    try {
      for (const imageId of imageIds) {
        await request(`/api/tasks/${task.task_id}/quality`, {
          method: 'POST',
          body: JSON.stringify({ image_id: imageId, quality_status: 'excluded' }),
        })
      }
      await refreshTask()
    } finally {
      setBusyAction('')
    }
  }

  return (
    <div className="surface">
      <div className="surface-title">
        {icon}
        <h3>{title}</h3>
      </div>
      {task ? (
        <>
          <div className="progress-row">
            <span>{task.message}</span>
            <strong>{task.progress}%</strong>
          </div>
          <div className="progress-track" aria-label={`${title}进度`}>
            <span style={{ width: `${task.progress}%` }} />
          </div>
          <div className="task-stats">
            <span>成功 {task.succeeded}</span>
            <span>失败 {task.failed}</span>
            {hasMissingDownloads ? <span>待补采 {Math.max(0, task.total - processedCount)}</span> : null}
            <span>总数 {task.total || '计算中'}</span>
          </div>
          <div className="task-controls">
            <button type="button" onClick={() => controlTask('pause')} disabled={busyAction === 'pause' || task.status !== 'running'}>
              暂停
            </button>
            <button type="button" onClick={() => controlTask('resume')} disabled={busyAction === 'resume' || (!hasMissingDownloads && task.status !== 'paused')}>
              {hasMissingDownloads ? '继续补采' : '继续'}
            </button>
            <button type="button" onClick={() => controlTask('cancel')} disabled={Boolean(busyAction) || ['completed', 'failed', 'canceled'].includes(task.status)}>
              取消
            </button>
          </div>
          {task.kind === 'download' ? (
            <button type="button" className="mini-action full task-action" onClick={retryFailed} disabled={busyAction === 'retry' || task.failed === 0 || task.status === 'running'}>
              {busyAction === 'retry' ? <Loader2 className="spin" size={15} aria-hidden="true" /> : <Download size={15} aria-hidden="true" />}
              重试失败
            </button>
          ) : null}
          {records.length ? (
            <div className="record-list">
              <div className="record-list-title">记录筛选</div>
              <div className="record-filters">
                <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)} aria-label="下载状态筛选">
                  <option value="all">全部状态</option>
                  {statusOptions.map((value) => <option key={value}>{value}</option>)}
                </select>
                <select value={qualityFilter} onChange={(event) => setQualityFilter(event.target.value)} aria-label="质量状态筛选">
                  <option value="all">全部质量</option>
                  {qualityOptions.map((value) => <option key={value}>{value}</option>)}
                </select>
                <select value={headingFilter} onChange={(event) => setHeadingFilter(event.target.value)} aria-label="方向筛选">
                  <option value="all">全部方向</option>
                  {headingOptions.map((value) => <option key={value} value={value}>{value}°</option>)}
                </select>
                <select value={roadFilter} onChange={(event) => setRoadFilter(event.target.value)} aria-label="道路筛选">
                  <option value="all">全部道路</option>
                  {roadOptions.map((value) => <option key={value}>{value}</option>)}
                </select>
              </div>
              {task.kind === 'download' ? (
                <button type="button" className="mini-action full task-action" onClick={batchExcludeVisible} disabled={busyAction === 'batch-exclude' || filteredRecords.length === 0}>
                  {busyAction === 'batch-exclude' ? <Loader2 className="spin" size={15} aria-hidden="true" /> : <CheckCircle2 size={15} aria-hidden="true" />}
                  批量排除当前筛选结果
                </button>
              ) : null}
              <div className="record-list-title">最近记录 {filteredRecords.length ? `(${filteredRecords.length})` : '(0)'}</div>
              {recentRecords.map((record) => {
                const imageId = String(record.image_id ?? '')
                return (
                  <div className="record-row" key={`${task.task_id}-${imageId}`}>
                    <div>
                      <strong>{imageId || record.point_id}</strong>
                      <span>
                        {record.point_id} · {record.heading ?? '-'}° · {record.status ?? record.gvi ?? ''}
                      </span>
                      {record.quality_status ? <em>{String(record.quality_status)}</em> : null}
                    </div>
                    {task.kind === 'download' && imageId ? (
                      <div className="record-actions">
                        <button type="button" onClick={() => markQuality(imageId, 'accepted')} disabled={Boolean(busyAction)}>
                          合格
                        </button>
                        <button type="button" onClick={() => markQuality(imageId, 'low_quality')} disabled={Boolean(busyAction)}>
                          低质
                        </button>
                        <button type="button" onClick={() => markQuality(imageId, 'excluded')} disabled={Boolean(busyAction)}>
                          排除
                        </button>
                      </div>
                    ) : null}
                  </div>
                )
              })}
            </div>
          ) : null}
        </>
      ) : (
        <p className="muted">等待创建任务。</p>
      )}
    </div>
  )
}

function CloudAccountCard({
  cloudUser,
  cloudEmail,
  cloudPassword,
  cloudAuthMessage,
  configured,
  compact = false,
  onEmailChange,
  onPasswordChange,
  onSignIn,
  onSignUp,
  onSignOut,
}: {
  cloudUser: { id: string; email?: string } | null
  cloudEmail: string
  cloudPassword: string
  cloudAuthMessage: string
  configured: boolean
  compact?: boolean
  onEmailChange: (value: string) => void
  onPasswordChange: (value: string) => void
  onSignIn: () => void
  onSignUp: () => void
  onSignOut: () => void
}) {
  return (
    <section className={compact ? 'cloud-card compact-cloud-card' : 'cloud-card'}>
      <div className="cloud-card-title">
        <Cloud size={17} aria-hidden="true" />
        <strong>云端账号</strong>
      </div>
      {!configured ? (
        <p className="inline-note">当前构建未配置 Supabase，公网登录和云端任务暂不可用。</p>
      ) : cloudUser ? (
        <>
          <div className="cloud-user-line">
            <UserRound size={16} aria-hidden="true" />
            <span>{cloudUser.email || '已登录'}</span>
          </div>
          <button type="button" className="mini-action full" onClick={onSignOut}>
            <LogOut size={15} aria-hidden="true" />
            退出登录
          </button>
        </>
      ) : (
        <>
          <input value={cloudEmail} onChange={(event) => onEmailChange(event.target.value)} placeholder="邮箱" />
          <input value={cloudPassword} onChange={(event) => onPasswordChange(event.target.value)} placeholder="密码" type="password" />
          <div className="cloud-auth-actions">
            <button type="button" className="mini-action" onClick={onSignIn}>
              登录
            </button>
            <button type="button" className="mini-action" onClick={onSignUp}>
              注册
            </button>
          </div>
        </>
      )}
      {cloudAuthMessage ? <p className="inline-note">{cloudAuthMessage}</p> : null}
    </section>
  )
}

export default App
