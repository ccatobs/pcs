status_fields = {
    'latp' : {
        'status_fields':   {
            'summary':    {
                'AzimuthMode':                          'Azimuth_mode',
                'AzimuthCurrentPosition':               'Azimuth_current_position',
                'AzimuthCurrentVelocity':               'Azimuth_current_velocity',
                'ElevationMode':                        'Elevation_mode',
                'ElevationCurrentPosition':             'Elevation_current_position',
                'ElevationCurrentVelocity':             'Elevation_current_velocity',
                'QtyOfFreeProgramTrackStackPositions':  'Free_upload_positions',
                'ElevationStowPinsStatus':              'Elevation_stowpin_status',
                },
            'position_errors':    {
                'AzimuthAveragePositionError':          'Azimuth_avg_position_error',
                'AzimuthPeakPositionError':             'Azimuth_peak_position_error',
                'ElevationAveragePositionError':        'Elevation_avg_position_error',
                'ElevationPeakPositionError':           'Elevation_peak_position_error',
                },
            'axis_limits':    {
                'AzimuthCCWLimit':                      'AzCCW_limit',
                'AzimuthCWLimit':                       'AzCW_limit',
                'ElevationCCWLimit':                    'ElCCW_limit',
                'ElevationCWLimit':                     'ElCW_limit',
                },
            'axis_faults_errors_overages':    {
                'AzimuthSummaryFault':                  'Azimuth_summary_fault',
                'ElevationSummaryFault':                'Elevation_summary_fault',
                },
            'axis_state':    {
                'AzimuthComputerDisabled':              'Azimuth_computer_disabled',
                'AzimuthAxisDisabled':                  'Azimuth_disabled',
                'AzimuthAxisInStop':                    'Azimuth_axis_stop',
                'AzimuthBrakesReleased':                'Azimuth_brakes_released',
                'AzimuthStopAtLCP':                     'Azimuth_stop_LCP',
                'AzimuthPowerOn':                       'Azimuth_power_on',
                'ElevationComputerDisabled':            'Elevation_computer_disabled',
                'ElevationAxisDisabled':                'Elevation_disabled',
                'ElevationAxisInStop':                  'Elevation_axis_stop',
                'ElevationBrakesReleased':              'Elevation_brakes_released',
                'ElevationAxisInStowPosition':          'Elevation_axis_stow_position',
                'ElevationStopAtLCP':                   'Elevation_stop_LCP',
                'ElevationPowerOn':                     'Elevation_power_on',
                },
            'commands':    {
                'AzimuthCommandedPosition':             'Azimuth_commanded_position',
                'ElevationCommandedPosition':           'Elevation_commanded_position',
                },
            'platform_status':    {
                'PCUOperation':                         'PCU_operation',
                'Remote':                               'Remote_mode',
                'ATLockOn':                             'ATLock_on',
                },
            'sun_avoidance':    {
                },
            'faults':    {
                },
            'shutter':    {
                },
            'hvac':    {
                },
            'corrections':    {
                },
            }
        },

    }

def allkeys(platform_type):
    all_keys = []
    pfd = status_fields[platform_type]['status_fields']
    for category in pfd.keys():
        for key in pfd[category].keys():
            all_keys.append(key)
    return all_keys
