from escpos.printer import Usb

# VendorID and ProductID for RONGTA printers may vary; run `lsusb` on Linux to get correct IDs
p = Usb(0xXXXX, 0xYYYY)  # Replace with actual VendorID and ProductID

# Print barcode
p.set(align='center')
p.text("Product: Widget\n")
p.barcode('123456789012', 'EAN13', width=2, height=100, pos='below', font='B')
p.cut()
