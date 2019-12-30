from cereal import car
from selfdrive.car import apply_std_steer_torque_limits
from selfdrive.car.hyundai.hyundaican import create_lkas11, create_clu11, \
                                             create_scc12, create_mdps12
from selfdrive.car.hyundai.values import CAR, Buttons
from opendbc.can.packer import CANPacker

# Steer torque limits

class SteerLimitParams:
  STEER_MAX = 408   # 409 is the max, 255 is stock
  STEER_DELTA_UP = 4
  STEER_DELTA_DOWN = 6
  STEER_DRIVER_ALLOWANCE = 50
  STEER_DRIVER_MULTIPLIER = 2
  STEER_DRIVER_FACTOR = 1
  
class LowSpeedSteerLimitParams(SteerLimitParams):
  STEER_MAX = 300
  STEER_DELTA_UP = 2
  STEER_DELTA_DOWN = 3
  STEER_DRIVER_ALLOWANCE = 50
  STEER_DRIVER_MULTIPLIER = 2
  STEER_DRIVER_FACTOR = 1

VisualAlert = car.CarControl.HUDControl.VisualAlert

def process_hud_alert(enabled, button_on, fingerprint, visual_alert, left_line,
                       right_line, left_lane_depart, right_lane_depart):

  hud_alert = 0
  if visual_alert == VisualAlert.steerRequired:
    hud_alert = 4

  # initialize to no line visible
  
  lane_visible = 1
  if not button_on:
    lane_visible = 0
  elif left_line and right_line or hud_alert:
    if enabled or hud_alert:
      lane_visible = 3
    else:
      lane_visible = 4
  elif left_line:
    lane_visible = 5
  elif right_line:
    lane_visible = 6

  # initialize to no warnings
  left_lane_warning = 0
  right_lane_warning = 0
  if left_lane_depart:
    left_lane_warning = 1 if fingerprint in [CAR.GENESIS , CAR.GENESIS_G90, CAR.GENESIS_G80] else 2
  if right_lane_depart:
    right_lane_warning = 1 if fingerprint in [CAR.GENESIS , CAR.GENESIS_G90, CAR.GENESIS_G80] else 2

  return hud_alert, lane_visible, left_lane_warning, right_lane_warning

class CarController():
  def __init__(self, dbc_name, car_fingerprint):
    self.apply_steer_last = 0
    self.car_fingerprint = car_fingerprint
    self.last_lead_distance = 0
    self.packer = CANPacker(dbc_name)
    self.steer_rate_limited = False
    self.resume_cnt = 0
    self.last_resume_frame = 0
    self.turning_signal_timer = 0
    self.scc12_cnt = 0
    self.lkas_button = 0 #TODO: make auto and fix bug
    self.longcontrol = 0 #TODO: make auto

  def update(self, enabled, CS, frame, actuators, pcm_cancel_cmd, visual_alert,
              left_line, right_line, left_lane_depart, right_lane_depart):

    ### Steering Torque
    new_steer = actuators.steer * SteerLimitParams.STEER_MAX
    if CS.v_ego < 10:
      apply_steer = apply_std_steer_torque_limits(new_steer, self.apply_steer_last, CS.steer_torque_driver, LowSpeedSteerLimitParams)
    else:
      apply_steer = apply_std_steer_torque_limits(new_steer, self.apply_steer_last, CS.steer_torque_driver, SteerLimitParams)
    self.steer_rate_limited = new_steer != apply_steer

    lkas_active = enabled and abs(CS.angle_steers) < 90. and (not self.lkas_button or CS.lkas_button_on)
    # Fix for sharp turns mdps fault and Genesis hard fault at low speed
    if CS.v_ego < 13.7 and self.car_fingerprint == CAR.GENESIS and not CS.mdps_bus:
      self.turning_signal_timer = 100
    if ((CS.left_blinker_flash or CS.right_blinker_flash) and CS.v_ego < 17.5): # Disable steering when blinker on and belwo ALC speed
      self.turning_signal_timer = 100  # Disable for 1.0 Seconds after blinker turned off
    if self.turning_signal_timer:
      lkas_active = 0
      self.turning_signal_timer -= 1

    if not lkas_active:
      apply_steer = 0

    steer_req = 1 if apply_steer else 0

    self.apply_steer_last = apply_steer

    hud_alert, lane_visible, left_lane_warning, right_lane_warning =\
            process_hud_alert(lkas_active, (not self.lkas_button or CS.lkas_button_on), self.car_fingerprint, visual_alert,
            left_line, right_line,left_lane_depart, right_lane_depart)

    clu11_speed = CS.clu11["CF_Clu_Vanz"]
    enabled_speed = 34 if CS.is_set_speed_in_mph  else 55
    if clu11_speed > enabled_speed or not lkas_active:
      enabled_speed = clu11_speed

    can_sends = []

    lkas11_cnt = frame % 0x10
    clu11_cnt = frame % 0x10
    mdps12_cnt = frame % 0x100
    self.scc12_cnt %= 15

    can_sends.append(create_lkas11(self.packer, self.car_fingerprint, 0, apply_steer, steer_req, lkas11_cnt, lkas_active,
                                   CS.lkas11, hud_alert, lane_visible, left_lane_depart, right_lane_depart, keep_stock=True))
    if CS.mdps_bus: # send lkas12 and clu11 to mdps if it is not on bus 0
      can_sends.append(create_lkas11(self.packer, self.car_fingerprint, CS.mdps_bus, apply_steer, steer_req, lkas11_cnt, lkas_active,
                                   CS.lkas11, hud_alert, lane_visible, left_lane_depart, right_lane_depart, keep_stock=True))
      can_sends.append(create_clu11(self.packer, CS.mdps_bus, CS.clu11, Buttons.NONE, enabled_speed, clu11_cnt))

    if pcm_cancel_cmd and self.longcontrol:
      can_sends.append(create_clu11(self.packer, CS.scc_bus, CS.clu11, Buttons.CANCEL, clu11_speed, clu11_cnt))
    elif self.lkas_button: # send mdps12 to LKAS to prevent LKAS error if no cancel cmd
      can_sends.append(create_mdps12(self.packer, self.car_fingerprint, mdps12_cnt, CS.mdps12))

    if CS.scc_bus and self.longcontrol and frame % 2: # send scc12 to car if SCC not on bus 0 and longcontrol enabled
      can_sends.append(create_scc12(self.packer, apply_accel, enabled, self.scc12_cnt, CS.scc12))
      self.scc12_cnt += 1

    if CS.stopped:
      # run only first time when the car stopped
      if self.last_lead_distance == 0:
        # get the lead distance from the Radar
        self.last_lead_distance = CS.lead_distance
        self.resume_cnt = 0
      # when lead car starts moving, create 6 RES msgs
      elif CS.lead_distance > self.last_lead_distance and (frame - self.last_resume_frame) > 5:
        can_sends.append(create_clu11(self.packer, CS.scc_bus, CS.clu11, Buttons.RES_ACCEL, clu11_speed, clu11_cnt))
        self.resume_cnt += 1
        # interval after 6 msgs
        if self.resume_cnt > 5:
          self.last_resume_frame = frame
          self.clu11_cnt = 0
    # reset lead distnce after the car starts moving
    elif self.last_lead_distance != 0:
      self.last_lead_distance = 0  

    return can_sends
