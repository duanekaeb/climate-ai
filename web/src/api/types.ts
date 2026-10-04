/* Generated from api/climate/api/schemas.py by web/scripts/gen-types.mjs. Do not edit by hand. */

export interface ApiTypes {
  AcceptInvitationBody?: AcceptInvitationBody
  AccessTokenOut?: AccessTokenOut
  AgentFinishBody?: AgentFinishBody
  AgentHeartbeatBody?: AgentHeartbeatBody
  AgentInfo?: AgentInfo
  AgentRunOut?: AgentRunOut
  AgentSettings?: AgentSettings
  AlertOut?: AlertOut
  ApiTokenCreateBody?: ApiTokenCreateBody
  ApiTokenCreated?: ApiTokenCreated
  ApiTokenOut?: ApiTokenOut
  ArmIn?: ArmIn
  AskBody?: AskBody
  AuditEventOut?: AuditEventOut
  AuthErrorDetail?: AuthErrorDetail
  AuthState?: AuthState
  BacktestBody?: BacktestBody
  BacktestOut?: BacktestOut
  BaselineOut?: BaselineOut
  ChangeOut?: ChangeOut
  ChangePasswordBody?: ChangePasswordBody
  Checkpoint?: Checkpoint
  ComfortBand?: ComfortBand
  ComfortRow?: ComfortRow
  ControlActionOut?: ControlActionOut
  ControlSettings?: ControlSettings
  ControllerInfo?: ControllerInfo
  Coupling?: Coupling
  CouplingPoint?: CouplingPoint
  DailyRuntime?: DailyRuntime
  DecisionBody?: DecisionBody
  DeviceBody?: DeviceBody
  DriftReport?: DriftReport
  DriftUnit?: DriftUnit
  EcobeeLoginBody?: EcobeeLoginBody
  EcobeeLoginResult?: EcobeeLoginResult
  EcobeeMapBody?: EcobeeMapBody
  EcobeeMfaBody?: EcobeeMfaBody
  EcobeeOriginal?: EcobeeOriginal
  EcobeeSetup?: EcobeeSetup
  EcobeeThermostatOut?: EcobeeThermostatOut
  ExperimentAnalysis?: ExperimentAnalysis
  ExperimentDayOut?: ExperimentDayOut
  ExperimentDecisionBody?: ExperimentDecisionBody
  ExperimentDetail?: ExperimentDetail
  ExperimentOut?: ExperimentOut
  GuardResult?: GuardResult
  HandbackInfo?: HandbackInfo
  HandbackStep?: HandbackStep
  HardLimits?: HardLimits
  Health?: Health
  HoldInfo?: HoldInfo
  HomekitCodeBody?: HomekitCodeBody
  HomekitDeviceOut?: HomekitDeviceOut
  HomekitInfo?: HomekitInfo
  HomekitPairBody?: HomekitPairBody
  HomekitSetup?: HomekitSetup
  HouseStatus?: HouseStatus
  Intraday?: Intraday
  IntradayPoint?: IntradayPoint
  IntradayUnit?: IntradayUnit
  InvitationInfo?: InvitationInfo
  InvitationOut?: InvitationOut
  JobOut?: JobOut
  LocationSettings?: LocationSettings
  LoginBody?: LoginBody
  ManualHoldBody?: ManualHoldBody
  MeOut?: MeOut
  ModeBody?: ModeBody
  ModelFitOut?: ModelFitOut
  NaturalEvent?: NaturalEvent
  NaturalExperiments?: NaturalExperiments
  OccupancySettings?: OccupancySettings
  OutdoorPoint?: OutdoorPoint
  PersonHold?: PersonHold
  PlanOut?: PlanOut
  PlanRow?: PlanRow
  PolicyParams?: PolicyParams
  PowerOut?: PowerOut
  PresenceBody?: PresenceBody
  ProposeExperimentBody?: ProposeExperimentBody
  ProposePolicyBody?: ProposePolicyBody
  PublishReportBody?: PublishReportBody
  ReauthBody?: ReauthBody
  ReportOut?: ReportOut
  ResetLinkOut?: ResetLinkOut
  ResetPasswordBody?: ResetPasswordBody
  RoomHistory?: RoomHistory
  RoomOut?: RoomOut
  RoomPoint?: RoomPoint
  RoomStatus?: RoomStatus
  RunBody?: RunBody
  Savings?: Savings
  SavingsByUnit?: SavingsByUnit
  SavingsDay?: SavingsDay
  Schedule?: Schedule
  SensorMapBody?: SensorMapBody
  SensorOut?: SensorOut
  SessionOut?: SessionOut
  SetpointPoint?: SetpointPoint
  SettingsOut?: SettingsOut
  SettingsUpdate?: SettingsUpdate
  SetupBody?: SetupBody
  SetupState?: SetupState
  SimPoint?: SimPoint
  SimulateBody?: SimulateBody
  SimulateOut?: SimulateOut
  SkipEventBody?: SkipEventBody
  SleepWindow?: SleepWindow
  SourceBody?: SourceBody
  SourceInfo?: SourceInfo
  SourceSettings?: SourceSettings
  ThermostatEvent?: ThermostatEvent
  UnitBody?: UnitBody
  UnitComfort?: UnitComfort
  UnitLive?: UnitLive
  UnitOut?: UnitOut
  UnitTarget?: UnitTarget
  UserCreateBody?: UserCreateBody
  UserCreateOut?: UserCreateOut
  UserOut?: UserOut
  UserUpdateBody?: UserUpdateBody
  UtilityEventOut?: UtilityEventOut
  UtilityEventSettings?: UtilityEventSettings
  UtilityInfo?: UtilityInfo
  Waterfall?: Waterfall
  WaterfallItem?: WaterfallItem
  WeatherNow?: WeatherNow
  WeatherOut?: WeatherOut
  WeatherPoint?: WeatherPoint
  WsEvent?: WsEvent
  WsTicketOut?: WsTicketOut
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "AcceptInvitationBody".
 */
export interface AcceptInvitationBody {
  display_name?: string
  password: string
  token: string
}
/**
 * Login / refresh / setup / accept-invitation / reset-password result. The access token is
 * kept in memory by the web app (never stored); the refresh token travels only as the HttpOnly
 * ``climate_refresh`` cookie (Path=/api/auth).
 *
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "AccessTokenOut".
 */
export interface AccessTokenOut {
  access_token: string
  expires_in: number
  token_type: 'bearer'
  user: UserOut
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "UserOut".
 */
export interface UserOut {
  created_at: string
  display_name: string
  id: number
  is_active: boolean
  last_login_at: string | null
  locked_until: string | null
  password_set: boolean
  role: 'admin' | 'member' | 'viewer'
  username: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "AgentFinishBody".
 */
export interface AgentFinishBody {
  error?: string | null
  model?: string | null
  not_before?: string | null
  num_turns?: number | null
  result_text?: string | null
  session_id?: string | null
  status: 'completed' | 'failed' | 'deferred'
  terminal_reason?: string | null
  usage?: {
    [k: string]: unknown
  } | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "AgentHeartbeatBody".
 */
export interface AgentHeartbeatBody {
  cli_version?: string | null
  detail?: {
    [k: string]: unknown
  }
  sdk_version?: string | null
  signed_in?: boolean | null
  token_expires_at?: string | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "AgentInfo".
 */
export interface AgentInfo {
  enabled: boolean
  last_beat_at: string | null
  last_run: AgentRunOut | null
  next_nightly_at: string | null
  sdk_version: string | null
  settings: AgentSettings
  signed_in: boolean | null
  token_expires_at: string | null
  token_warning: string | null
  triggered_today: number
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "AgentRunOut".
 */
export interface AgentRunOut {
  created_at: string
  error: string | null
  finished_at: string | null
  id: number
  kind: 'nightly' | 'weekly' | 'triggered' | 'chat' | 'signin_check'
  model: string | null
  not_before: string | null
  num_turns: number | null
  prompt: string
  requested_by: string
  result_text: string | null
  session_id: string | null
  started_at: string | null
  status: 'queued' | 'running' | 'completed' | 'failed' | 'deferred' | 'cancelled'
  terminal_reason: string | null
  trigger: {
    [k: string]: unknown
  } | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "AgentSettings".
 */
export interface AgentSettings {
  enabled: boolean
  max_triggered_per_day: number
  nightly_time: string
  weekly_day: number
  weekly_time: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "AlertOut".
 */
export interface AlertOut {
  body: string
  id: number
  kind: string
  level: 'info' | 'warn' | 'error'
  resolved_at: string | null
  title: string
  ts: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ApiTokenCreateBody".
 */
export interface ApiTokenCreateBody {
  expires_in_days?: number | null
  local_only?: boolean
  name: string
  role?: 'agent' | 'viewer' | 'member'
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ApiTokenCreated".
 */
export interface ApiTokenCreated {
  created_at: string
  created_by: string | null
  expires_at: string | null
  id: number
  last_used_at: string | null
  last_used_ip: string | null
  local_only: boolean
  name: string
  revoked_at: string | null
  role: 'agent' | 'viewer' | 'member'
  token: string
  token_hint: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ApiTokenOut".
 */
export interface ApiTokenOut {
  created_at: string
  created_by: string | null
  expires_at: string | null
  id: number
  last_used_at: string | null
  last_used_ip: string | null
  local_only: boolean
  name: string
  revoked_at: string | null
  role: 'agent' | 'viewer' | 'member'
  token_hint: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ArmIn".
 */
export interface ArmIn {
  key: string
  label: string
  params: {
    [k: string]: unknown
  }
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "AskBody".
 */
export interface AskBody {
  question: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "AuditEventOut".
 */
export interface AuditEventOut {
  actor_label: string
  actor_role: string | null
  actor_type: 'user' | 'api_token' | 'system'
  event_type: string
  id: number
  ip: string | null
  payload: {
    [k: string]: unknown
  }
  target_id: string | null
  target_type: string | null
  ts: string
}
/**
 * ``detail`` of a 401/403/423 from the auth layer, so clients can react precisely:
 * TOKEN_EXPIRED (refresh and replay once), SESSION_REVOKED / NOT_AUTHENTICATED (go to login),
 * REAUTHENTICATION_REQUIRED (ask for the password, then replay), ACCOUNT_LOCKED,
 * INVALID_CREDENTIALS, FORBIDDEN, SETUP_NOT_ALLOWED, INVALID_RESET_TOKEN, INVALID_INVITATION.
 *
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "AuthErrorDetail".
 */
export interface AuthErrorDetail {
  code: string
  message: string
}
/**
 * GET /api/auth/state (public). ``password_set`` False = no user exists yet: show the
 * first-run screen, which only works from a private address unless allowed (``setup_allowed``).
 *
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "AuthState".
 */
export interface AuthState {
  authenticated: boolean
  password_set: boolean
  role: ('admin' | 'member' | 'viewer' | 'agent') | null
  setup_allowed: boolean
  user: UserOut | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "BacktestBody".
 */
export interface BacktestBody {
  days?: number
  params?: {
    [k: string]: unknown
  }
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "BacktestOut".
 */
export interface BacktestOut {
  beats_model_uncertainty: boolean
  candidate_runtime_min: number | null
  ci90_pct: [unknown, unknown] | null
  comfort_violation_min_candidate: number | null
  comfort_violation_min_current: number | null
  current_runtime_min: number | null
  days: number
  delta_pct: number | null
  model: 'rc' | 'rule_of_thumb'
  note: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "BaselineOut".
 */
export interface BaselineOut {
  balance_point_f: number
  cvrmse: number
  fitted_at: string
  intercept_min: number
  mode: 'cool' | 'heat'
  n_days: number
  nmbe: number
  passes: boolean
  r2: number
  slope_min_per_dd: number
  train_end: string
  train_start: string
  unit_key: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ChangeOut".
 */
export interface ChangeOut {
  created_at: string
  decided_at: string | null
  decided_by: string | null
  decision_reason: string | null
  gates: {
    [k: string]: unknown
  }
  id: number
  kind: 'policy' | 'model' | 'experiment'
  needs: 'nothing' | 'claude' | 'owner'
  payload: {
    [k: string]: unknown
  }
  proposed_by: 'model' | 'claude' | 'owner'
  rationale: string
  shadow_start: string | null
  status: string
  title: string
  trial_end: string | null
  trial_start: string | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ChangePasswordBody".
 */
export interface ChangePasswordBody {
  current_password: string
  new_password: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "Checkpoint".
 */
export interface Checkpoint {
  alpha_spent: number
  day: number
  info_fraction: number
  z_crit: number
}
/**
 * Setpoints for one period. heat_f < cool_f by at least the thermostat's min delta.
 *
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ComfortBand".
 */
export interface ComfortBand {
  cool_f: number
  heat_f: number
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ComfortRow".
 */
export interface ComfortRow {
  in_band_pct: number | null
  occupied_min: number
  room_key: string
  worst_excursion_f: number | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ControlActionOut".
 */
export interface ControlActionOut {
  action: string
  actor: string
  before: {
    [k: string]: unknown
  } | null
  channel: string
  completed_at: string | null
  error: string | null
  id: number
  mode: string
  readback: {
    [k: string]: unknown
  } | null
  readback_ok: boolean | null
  reason: string
  request: {
    [k: string]: unknown
  } | null
  rule: string | null
  status: string
  ts: string
  unit_key: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ControlSettings".
 */
export interface ControlSettings {
  act_units: string[]
  comfort: {
    [k: string]: UnitComfort
  }
  hold_hours: number
  limits: HardLimits
  manual_hold_reminder_hours: number
  mode: 'off' | 'suggest' | 'act'
  resume_backoff_hours: number
  schedule: Schedule
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "UnitComfort".
 */
export interface UnitComfort {
  away: ComfortBand
  day: ComfortBand
  night: ComfortBand
}
/**
 * Enforced in code on every write, whoever asked for it. Owner-editable only.
 *
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "HardLimits".
 */
export interface HardLimits {
  max_cool_f: number
  max_heat_f: number
  max_hold_hours: number
  max_indoor_rh: number
  max_step_f: number
  min_cool_f: number
  min_deadband_f: number
  min_heat_f: number
  min_hold_hours: number
  min_minutes_between_changes: number
  min_run_minutes: number
}
/**
 * Household rhythm used for room priorities (blueprint §3). Days: Monday=0 .. Sunday=6.
 *
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "Schedule".
 */
export interface Schedule {
  evening_start: string
  office_days: number[]
  office_end: string
  office_start: string
  school_days: number[]
  school_end: string
  school_start: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ControllerInfo".
 */
export interface ControllerInfo {
  last_tick_at: string | null
  mode: 'off' | 'suggest' | 'act'
  policy: PolicyParams
  policy_version_id: number | null
}
/**
 * Tunable policy. Defaults are the blueprint's starting values.
 *
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "PolicyParams".
 */
export interface PolicyParams {
  bed_wing_independent: boolean
  linked_floors_enabled: boolean
  linked_heat_gap_f: number
  linked_offset_f: number
  precool_degrees_f: number
  precool_enabled: boolean
  precool_min_forecast_high_f: number
  precool_start_hour: number
  recovery_lead_min: number
  setback_gap_f: number
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "Coupling".
 */
export interface Coupling {
  ci90: [unknown, unknown] | null
  coef_min_per_degf: number | null
  days: number
  interpretation: string
  n_hours: number
  placebo_ci90: [unknown, unknown] | null
  placebo_coef: number | null
  points: CouplingPoint[]
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "CouplingPoint".
 */
export interface CouplingPoint {
  main_minus_up_f: number
  outdoor_f: number | null
  shortwave_wm2: number | null
  ts: string
  up_duty_pct: number
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "DailyRuntime".
 */
export interface DailyRuntime {
  aux_min: number
  cdd65: number | null
  cool_min: number
  date: string
  expected_min: number | null
  fan_min: number
  hdd65: number | null
  heat_min: number
  maxed_min: number
  mode: ('cool' | 'heat') | null
  outdoor_max_f: number | null
  outdoor_mean_f: number | null
  unit_key: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "DecisionBody".
 */
export interface DecisionBody {
  decision: 'approve' | 'hold' | 'reject'
  reason: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "DeviceBody".
 */
export interface DeviceBody {
  device_id: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "DriftReport".
 */
export interface DriftReport {
  note: string
  units: DriftUnit[]
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "DriftUnit".
 */
export interface DriftUnit {
  drifting: boolean
  mode: 'cool' | 'heat'
  recent_days: number
  resid_mean_pct: number
  unit_key: string
  z: number
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "EcobeeLoginBody".
 */
export interface EcobeeLoginBody {
  email: string
  password: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "EcobeeLoginResult".
 */
export interface EcobeeLoginResult {
  error: string | null
  mfa_type: string | null
  status: 'signed_in' | 'mfa_required' | 'error'
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "EcobeeMapBody".
 */
export interface EcobeeMapBody {
  identifier: string
  unit_key: string | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "EcobeeMfaBody".
 */
export interface EcobeeMfaBody {
  code: string
}
/**
 * A unit's ecobee settings as they were before the controller first changed them, so
 * "Hand back to ecobee" can restore them. Captured once per unit (first ecobee snapshot,
 * and again just before any first write that would change one of them if still missing).
 *
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "EcobeeOriginal".
 */
export interface EcobeeOriginal {
  auto_away: boolean | null
  captured_at: string
  follow_me: boolean | null
  home_sensors: string[] | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "EcobeeSetup".
 */
export interface EcobeeSetup {
  last_error: string | null
  mfa_pending: boolean
  mfa_type: string | null
  signed_in: boolean
  thermostats: EcobeeThermostatOut[]
  web_client_id: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "EcobeeThermostatOut".
 */
export interface EcobeeThermostatOut {
  dr_accept: string | null
  enrolled: boolean | null
  identifier: string
  last_seen_at: string
  model_number: string | null
  name: string
  sensors: {
    [k: string]: unknown
  }[]
  unit_key: string | null
  utility: UtilityInfo | null
}
/**
 * The utility ecobee associates the thermostat with (includeUtility). Its presence, or a
 * demand-response event, is how the app tells the owner a thermostat is enrolled.
 *
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "UtilityInfo".
 */
export interface UtilityInfo {
  email: string | null
  name: string
  phone: string | null
  web: string | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ExperimentAnalysis".
 */
export interface ExperimentAnalysis {
  checkpoint_reached: number | null
  ci_high_pct: number | null
  ci_low_pct: number | null
  days_observed: number
  decision: 'continue' | 'stop_win' | 'stop_futile' | 'inconclusive' | 'not_started'
  effect_pct: number | null
  note: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ExperimentDayOut".
 */
export interface ExperimentDayOut {
  actual_min: number | null
  arm: string
  day: string
  expected_min: number | null
  included: boolean
  note: string | null
  residual_min: number | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ExperimentDecisionBody".
 */
export interface ExperimentDecisionBody {
  decision: 'approve' | 'reject' | 'stop'
  reason: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ExperimentDetail".
 */
export interface ExperimentDetail {
  analysis: ExperimentAnalysis
  experiment: ExperimentOut
  schedule: ExperimentDayOut[]
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ExperimentOut".
 */
export interface ExperimentOut {
  alpha: number
  arms: ArmIn[]
  block_days: number
  checkpoints: Checkpoint[]
  created_at: string
  end_date: string | null
  hypothesis: string
  id: number
  metric: string
  n_days: number
  name: string
  proposed_by: string
  start_date: string | null
  status: 'proposed' | 'approved' | 'running' | 'stopped' | 'completed' | 'rejected'
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "GuardResult".
 */
export interface GuardResult {
  blocked_reason: string | null
  clamped: boolean
  cool_f: number
  heat_f: number
  ok: boolean
  violations: string[]
}
/**
 * What "Hand back to ecobee" restores (GET) and, after a run, what it did.
 *
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "HandbackInfo".
 */
export interface HandbackInfo {
  last_job: JobOut | null
  mode: 'off' | 'suggest' | 'act'
  original: {
    [k: string]: EcobeeOriginal
  }
  steps: HandbackStep[]
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "JobOut".
 */
export interface JobOut {
  created_at: string
  error: string | null
  finished_at: string | null
  id: number
  kind: string
  params: {
    [k: string]: unknown
  }
  result: {
    [k: string]: unknown
  } | null
  status: 'queued' | 'running' | 'done' | 'failed'
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "HandbackStep".
 */
export interface HandbackStep {
  detail: string
  ok: boolean
  unit_key: string | null
  what: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "Health".
 */
export interface Health {
  db: boolean
  ok: boolean
  version: string
}
/**
 * What overrides the thermostat's schedule right now (the top running ecobee event).
 *
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "HoldInfo".
 */
export interface HoldInfo {
  climate_ref: string | null
  cool_f: number | null
  cool_offset_f: number | null
  end: string | null
  event_name: string | null
  heat_f: number | null
  heat_offset_f: number | null
  hold_type: string | null
  is_optional: boolean | null
  is_relative: boolean
  kind: 'temperature' | 'climate'
  link_ref: string | null
  set_by_us: boolean
  start: string | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "HomekitCodeBody".
 */
export interface HomekitCodeBody {
  code: string
  device_id: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "HomekitDeviceOut".
 */
export interface HomekitDeviceOut {
  accessories:
    | {
        [k: string]: unknown
      }[]
    | null
  address: string | null
  alias: string | null
  device_id: string
  last_seen_at: string | null
  model: string | null
  name: string
  online: boolean
  pairing_error: string | null
  pairing_state: string
  unit_key: string | null
  unpaired: boolean
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "HomekitInfo".
 */
export interface HomekitInfo {
  enabled: boolean
  last_beat_at: string | null
  online: boolean
  paired: number
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "HomekitPairBody".
 */
export interface HomekitPairBody {
  alias: string
  device_id: string
  unit_key: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "HomekitSetup".
 */
export interface HomekitSetup {
  devices: HomekitDeviceOut[]
  enabled: boolean
  service_online: boolean
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "HouseStatus".
 */
export interface HouseStatus {
  agent: AgentInfo
  alerts: AlertOut[]
  controller: ControllerInfo
  homekit: HomekitInfo
  house_empty: boolean
  house_empty_reason: string
  location_confirmed: boolean
  now: string
  rooms: RoomStatus[]
  source: SourceInfo
  tz: string
  units: UnitLive[]
  weather: WeatherNow | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "RoomStatus".
 */
export interface RoomStatus {
  confidence: number
  floor: string
  has_comfort_target: boolean
  has_sensor: boolean
  humidity: number | null
  is_priority: boolean
  is_sleep_room: boolean
  name: string
  offset_f: number | null
  reason: string
  room_key: string
  seconds_since_motion: number | null
  sensor_keys: string[]
  since: string | null
  stale: boolean
  state: 'occupied' | 'asleep' | 'empty' | 'unknown' | 'no_target'
  temp_f: number | null
  unit_key: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "SourceInfo".
 */
export interface SourceInfo {
  detail: string
  kind: 'simulator' | 'ecobee'
  last_success_at: string | null
  ok: boolean
  signed_in: boolean | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "UnitLive".
 */
export interface UnitLive {
  age_s: number | null
  call: 'cool' | 'heat' | 'fan' | 'idle' | 'unknown'
  climate_ref: string | null
  connected: boolean
  cool_sp_f: number | null
  duty_last_hour_pct: number | null
  heat_sp_f: number | null
  hold: HoldInfo | null
  hold_label: string | null
  hold_owner:
    ('controller' | 'person' | 'app' | 'utility' | 'vacation' | 'ecobee_auto' | 'unknown_event') | null
  hvac_mode: string | null
  maxed_minutes_today: number
  name: string
  person_hold: PersonHold | null
  resume_backoff_until: string | null
  running: string[]
  target: UnitTarget | null
  today_runtime_min: number
  unit_key: string
  upcoming_events: ThermostatEvent[]
  utility_event: UtilityEventOut | null
  zone_humidity: number | null
  zone_temp_f: number | null
}
/**
 * A hold a person set. It always wins: the controller writes nothing to the unit while
 * it runs, however long that is (``climate.state`` detects it; guardrails blocks on it).
 *
 * ``by``: 'app' = the owner's hold from this app's hold form; 'thermostat' = anything else a
 * person did (at the wall, in the ecobee app, Apple Home, Siri: ecobee does not say which).
 *
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "PersonHold".
 */
export interface PersonHold {
  by: 'thermostat' | 'app'
  climate_ref: string | null
  cool_f: number | null
  detection_id: number | null
  first_seen: string
  heat_f: number | null
  hold_type: string | null
  since: string
  until: string | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "UnitTarget".
 */
export interface UnitTarget {
  cool_f: number
  desired: 'hold' | 'program'
  heat_f: number
  hold_end_by: string | null
  priority_room: string | null
  reason: string
  rule:
    | 'comfort'
    | 'sleep'
    | 'linked_floors'
    | 'house_setback'
    | 'recovery'
    | 'precool'
    | 'independent'
    | 'hold_off'
    | 'event_prep'
  unit_key: string
}
/**
 * A demand-response or vacation event on a thermostat, running or scheduled ahead
 * (ecobee lists announced utility events before they start). Times are UTC.
 *
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ThermostatEvent".
 */
export interface ThermostatEvent {
  cool_f: number | null
  cool_offset_f: number | null
  duty_cycle_pct: number | null
  end: string | null
  event_type: string
  heat_f: number | null
  heat_offset_f: number | null
  is_cool_off: boolean
  is_heat_off: boolean
  is_optional: boolean | null
  is_relative: boolean
  link_ref: string | null
  name: string | null
  running: boolean
  start: string | null
}
/**
 * A utility energy-saving (demand-response) event on one thermostat.
 *
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "UtilityEventOut".
 */
export interface UtilityEventOut {
  can_skip: boolean
  change_label: string
  cool_f: number | null
  cool_offset_f: number | null
  duty_cycle_pct: number | null
  end_at: string | null
  ended_at: string | null
  event_key: string
  event_type: string
  first_seen_at: string
  heat_f: number | null
  heat_offset_f: number | null
  id: number
  is_optional: boolean | null
  is_relative: boolean
  name: string | null
  prep_label: string | null
  skip: ('requested' | 'done' | 'failed' | 'refused') | null
  skip_by: ('owner' | 'rule') | null
  skip_done_at: string | null
  skip_reason: string | null
  skip_requested_at: string | null
  start_at: string | null
  started_at: string | null
  status: 'announced' | 'running' | 'ended' | 'cancelled' | 'opted_out'
  unit_key: string
  unit_name: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "WeatherNow".
 */
export interface WeatherNow {
  attribution: string
  cloud_cover: number | null
  forecast_high_f: number | null
  forecast_low_f: number | null
  rh: number | null
  shortwave_wm2: number | null
  source: string
  temp_f: number | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "Intraday".
 */
export interface Intraday {
  date: string
  outdoor: OutdoorPoint[]
  units: IntradayUnit[]
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "OutdoorPoint".
 */
export interface OutdoorPoint {
  temp_f: number | null
  ts: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "IntradayUnit".
 */
export interface IntradayUnit {
  points: IntradayPoint[]
  unit_key: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "IntradayPoint".
 */
export interface IntradayPoint {
  aux_s: number
  cool_s: number
  cool_sp_f: number | null
  fan_s: number
  heat_s: number
  heat_sp_f: number | null
  ts: string
  zone_temp_f: number | null
}
/**
 * GET /api/auth/invitation?token=… (public): what the invite is for.
 *
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "InvitationInfo".
 */
export interface InvitationInfo {
  expires_at: string
  role: 'admin' | 'member' | 'viewer'
  username: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "InvitationOut".
 */
export interface InvitationOut {
  accepted_at: string | null
  created_at: string
  expires_at: string
  id: number
  revoked_at: string | null
  role: 'admin' | 'member' | 'viewer'
  username: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "LocationSettings".
 */
export interface LocationSettings {
  confirmed: boolean
  label: string | null
  lat: number | null
  lon: number | null
  tz: string
  zip: string | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "LoginBody".
 */
export interface LoginBody {
  device_name?: string
  password: string
  username: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ManualHoldBody".
 */
export interface ManualHoldBody {
  cool_f: number
  heat_f: number
  hours?: number
  unit_key: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "MeOut".
 */
export interface MeOut {
  recently_authenticated: boolean
  role: 'admin' | 'member' | 'viewer' | 'agent'
  session_id: number | null
  token_id: number | null
  user: UserOut | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ModeBody".
 */
export interface ModeBody {
  mode: 'off' | 'suggest' | 'act'
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ModelFitOut".
 */
export interface ModelFitOut {
  created_at: string
  id: number
  kind: 'baseline' | 'rc' | 'room_offsets' | 'occupancy_priors'
  metrics: {
    [k: string]: unknown
  }
  mode: string | null
  notes: string | null
  params: {
    [k: string]: unknown
  }
  status: string
  train_end: string | null
  train_start: string | null
  unit_key: string | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "NaturalEvent".
 */
export interface NaturalEvent {
  date: string
  expected_up_runtime_min: number | null
  main_floor_float_f: number
  up_runtime_min: number
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "NaturalExperiments".
 */
export interface NaturalExperiments {
  bed_wing_ci90: [unknown, unknown] | null
  bed_wing_estimate: number | null
  ci90: [unknown, unknown] | null
  controls_ok: boolean | null
  days: number
  estimate_min_per_event: number | null
  events: NaturalEvent[]
  note: string
  placebo_ci90: [unknown, unknown] | null
  placebo_estimate: number | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "OccupancySettings".
 */
export interface OccupancySettings {
  empty_after_min: number
  house_empty_after_min: number
  phones_away: boolean | null
  phones_updated_at: string | null
  sleep_windows: {
    [k: string]: SleepWindow[]
  }
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "SleepWindow".
 */
export interface SleepWindow {
  days: number[]
  end: string
  start: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "PlanOut".
 */
export interface PlanOut {
  at: string
  mode: 'off' | 'suggest' | 'act'
  rows: PlanRow[]
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "PlanRow".
 */
export interface PlanRow {
  current_cool_f: number | null
  current_heat_f: number | null
  guard: GuardResult
  target: UnitTarget
  would_write: boolean
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "PowerOut".
 */
export interface PowerOut {
  alpha: number
  days_per_arm: number | null
  effect_pct: number
  note: string
  power: number
  resid_cv: number | null
  total_days: number | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "PresenceBody".
 */
export interface PresenceBody {
  phones_away: boolean | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ProposeExperimentBody".
 */
export interface ProposeExperimentBody {
  alpha?: number
  /**
   * @minItems 2
   * @maxItems 3
   */
  arms: [ArmIn, ArmIn] | [ArmIn, ArmIn, ArmIn]
  block_days?: number
  hypothesis: string
  n_checkpoints?: number
  n_days?: number
  name: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ProposePolicyBody".
 */
export interface ProposePolicyBody {
  params: {
    [k: string]: unknown
  }
  rationale?: string
  title: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "PublishReportBody".
 */
export interface PublishReportBody {
  agent_run_id?: number | null
  body_md: string
  data?: {
    [k: string]: unknown
  }
  kind: 'daily' | 'weekly' | 'nightly' | 'anomaly' | 'note'
  period_end?: string | null
  period_start?: string | null
  title: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ReauthBody".
 */
export interface ReauthBody {
  password: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ReportOut".
 */
export interface ReportOut {
  agent_run_id: number | null
  author: 'system' | 'claude'
  body_md: string
  created_at: string
  data: {
    [k: string]: unknown
  }
  id: number
  kind: 'daily' | 'weekly' | 'nightly' | 'anomaly' | 'note'
  period_end: string | null
  period_start: string | null
  title: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ResetLinkOut".
 */
export interface ResetLinkOut {
  expires_at: string
  url: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "ResetPasswordBody".
 */
export interface ResetPasswordBody {
  new_password: string
  token: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "RoomHistory".
 */
export interface RoomHistory {
  hours: number
  points: RoomPoint[]
  room_key: string
  setpoints: SetpointPoint[]
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "RoomPoint".
 */
export interface RoomPoint {
  occupied: boolean | null
  state: string | null
  temp_f: number | null
  ts: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "SetpointPoint".
 */
export interface SetpointPoint {
  cool_sp_f: number | null
  heat_sp_f: number | null
  ts: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "RoomOut".
 */
export interface RoomOut {
  floor: string
  has_comfort_target: boolean
  has_sensor: boolean
  is_sleep_room: boolean
  key: string
  name: string
  notes: string | null
  unit_key: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "RunBody".
 */
export interface RunBody {
  kind: 'nightly' | 'weekly'
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "Savings".
 */
export interface Savings {
  actual_min: number
  baseline_ok: boolean
  by_unit: SavingsByUnit[]
  ci90_high_pct: number | null
  ci90_low_pct: number | null
  days: SavingsDay[]
  end: string
  expected_min: number | null
  n_days: number
  note: string
  savings_min: number | null
  savings_pct: number | null
  start: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "SavingsByUnit".
 */
export interface SavingsByUnit {
  actual_min: number
  expected_min: number | null
  savings_pct: number | null
  unit_key: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "SavingsDay".
 */
export interface SavingsDay {
  actual_min: number
  date: string
  expected_min: number | null
  outdoor_mean_f: number | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "SensorMapBody".
 */
export interface SensorMapBody {
  ecobee_sensor_id?: string | null
  homekit_aid?: number | null
  sensor_key: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "SensorOut".
 */
export interface SensorOut {
  ecobee_sensor_id: string | null
  has_humidity: boolean
  has_occupancy: boolean
  homekit_aid: number | null
  key: string
  kind: string
  name: string
  room_key: string
  unit_key: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "SessionOut".
 */
export interface SessionOut {
  created_at: string
  current: boolean
  device_name: string
  expires_at: string
  id: number
  ip: string | null
  last_seen_at: string
  user_agent: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "SettingsOut".
 */
export interface SettingsOut {
  agent: AgentSettings
  control: ControlSettings
  location: LocationSettings
  occupancy: OccupancySettings
  owner_only_params: string[]
  policy: PolicyParams
  policy_version_id: number | null
  signoff_ranges: {
    /**
     * @minItems 2
     * @maxItems 2
     */
    [k: string]: [unknown, unknown]
  }
  utility_events: UtilityEventSettings
}
/**
 * What the app does around utility energy-saving (demand-response) events.
 *
 * The app never counteracts an event while staying in it: during a running event the
 * controller stands down. The only ways out are honest opt-outs (ecobee records them and
 * the utility sees them): the owner's "Skip this event", or a skip rule below. Pre-
 * conditioning happens only BEFORE an announced event, and its hold always ends at least
 * ``precondition_end_gap_min`` before the event starts, so the event is applied to the
 * normal setpoint, never to a lowered (or raised) one.
 *
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "UtilityEventSettings".
 */
export interface UtilityEventSettings {
  alerts: boolean
  auto_skip: boolean
  precondition: boolean
  precondition_degrees_f: number
  precondition_end_gap_min: number
  precondition_hours: number
  skip_above_f: number | null
  skip_below_f: number | null
  skip_when_asleep: boolean
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "SettingsUpdate".
 */
export interface SettingsUpdate {
  agent?: AgentSettings | null
  control?: ControlSettings | null
  location?: LocationSettings | null
  occupancy?: OccupancySettings | null
  utility_events?: UtilityEventSettings | null
}
/**
 * First-run: create the first admin (only while no user exists).
 *
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "SetupBody".
 */
export interface SetupBody {
  display_name?: string
  password: string
  username: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "SetupState".
 */
export interface SetupState {
  ecobee: EcobeeSetup
  homekit: HomekitSetup
  location: LocationSettings
  rooms: RoomOut[]
  secrets_ok: boolean
  sensors: SensorOut[]
  source: SourceSettings
  units: UnitOut[]
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "SourceSettings".
 */
export interface SourceSettings {
  cloud_circuit_open_until: string | null
  homekit_enabled: boolean
  kind: 'simulator' | 'ecobee'
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "UnitOut".
 */
export interface UnitOut {
  ecobee_identifier: string | null
  equipment: {
    [k: string]: unknown
  }
  homekit_device_id: string | null
  key: string
  name: string
  thermostat_model: string | null
  thermostat_room_key: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "SimPoint".
 */
export interface SimPoint {
  runtime_s: number
  temp_f: number
  ts: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "SimulateBody".
 */
export interface SimulateBody {
  date?: string | null
  params?: {
    [k: string]: unknown
  }
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "SimulateOut".
 */
export interface SimulateOut {
  date: string
  model: 'rc' | 'rule_of_thumb'
  note: string
  total_runtime_min: number
  units: {
    [k: string]: SimPoint[]
  }
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "SkipEventBody".
 */
export interface SkipEventBody {
  all_units?: boolean
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "SourceBody".
 */
export interface SourceBody {
  homekit_enabled?: boolean
  kind: 'simulator' | 'ecobee'
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "UnitBody".
 */
export interface UnitBody {
  unit_key: string
}
/**
 * Admin: create a user. With ``password`` the account is ready now (a temporary password
 * the person changes); without it an invitation link is returned (shown once).
 *
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "UserCreateBody".
 */
export interface UserCreateBody {
  display_name?: string
  password?: string | null
  role?: 'admin' | 'member' | 'viewer'
  username: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "UserCreateOut".
 */
export interface UserCreateOut {
  invitation_expires_at: string | null
  invitation_url: string | null
  user: UserOut | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "UserUpdateBody".
 */
export interface UserUpdateBody {
  display_name?: string | null
  is_active?: boolean | null
  role?: ('admin' | 'member' | 'viewer') | null
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "Waterfall".
 */
export interface Waterfall {
  items: WaterfallItem[]
  note: string
  prev_week_start: string
  strategy_ci90_min: [unknown, unknown] | null
  week_start: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "WaterfallItem".
 */
export interface WaterfallItem {
  kind: 'total' | 'delta'
  label: string
  minutes: number
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "WeatherOut".
 */
export interface WeatherOut {
  attribution: string
  points: WeatherPoint[]
  source: string
}
/**
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "WeatherPoint".
 */
export interface WeatherPoint {
  cloud_cover: number | null
  kind: 'observed' | 'forecast'
  rh: number | null
  shortwave_wm2: number | null
  temp_f: number | null
  ts: string
}
/**
 * Pushed on /api/ws. Clients refetch what changed; payloads stay tiny.
 *
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "WsEvent".
 */
export interface WsEvent {
  id: number | null
  type: 'status' | 'action' | 'agent_run' | 'alert' | 'homekit' | 'change' | 'report' | 'ping'
}
/**
 * POST /api/auth/ws-ticket: a single-use ticket for /api/ws?ticket=… (browsers cannot set
 * a bearer header on a WebSocket).
 *
 * This interface was referenced by `ApiTypes`'s JSON-Schema
 * via the `definition` "WsTicketOut".
 */
export interface WsTicketOut {
  expires_in: number
  ticket: string
}
