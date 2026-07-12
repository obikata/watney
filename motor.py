class Motor:
    def __init__(self, gpio, pwmFrequency, forwardPin, reversePin, trimOffset, name):
        self.gpio = gpio
        self.pwmFrequency = pwmFrequency
        self.forwardPin = forwardPin
        self.reversePin = reversePin
        self.trimOffset = trimOffset
        self.name = name
        self.__initMotor()

    def __initMotor(self):
        import pigpio
        self.__pigpio = pigpio
        self.stop()

    def __pinLow(self, pin):
        self.gpio.set_PWM_dutycycle(pin, 0)
        self.gpio.set_mode(pin, self.__pigpio.OUTPUT)
        self.gpio.write(pin, 0)

    def __pinPWM(self, pin, dutyCycle):
        self.gpio.set_PWM_frequency(pin, self.pwmFrequency)
        self.gpio.set_PWM_range(pin, 100)
        self.gpio.set_PWM_dutycycle(pin, dutyCycle)

    def stop(self):
        self.__pinLow(self.forwardPin)
        self.__pinLow(self.reversePin)

    def setMotion(self, dutyCycle):
        self.stop()
        if dutyCycle != 0:
            trimmedDutyCycle = dutyCycle * self.trimOffset
            if trimmedDutyCycle > 0:
                self.__pinLow(self.reversePin)
                self.__pinPWM(self.forwardPin, trimmedDutyCycle)
            else:
                self.__pinLow(self.forwardPin)
                self.__pinPWM(self.reversePin, trimmedDutyCycle * -1)
            return True
        else:
            return False
            
            