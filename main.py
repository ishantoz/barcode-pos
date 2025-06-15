import usb.core, usb.util

VENDOR_ID = 0x0fe6
PRODUCT_ID = 0x8800

printer = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
if printer is None:
    raise ValueError("Printer not found.")

if printer.is_kernel_driver_active(0):
    printer.detach_kernel_driver(0)

printer.set_configuration()
cfg = printer.get_active_configuration()
intf = cfg[(0, 0)]

ep_out = usb.util.find_descriptor(
    intf,
    custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
)
if ep_out is None:
    raise ValueError("No USB OUT endpoint found.")

# ——— Minimal TSPL (no HOME, no extra resets) ———
label = (
    # 1) Define size and gap
    "SIZE 45 mm, 35 mm\r\n" +
	"GAP 2 mm, 0 mm\r\n" +
	"DIRECTION 1\r\n" +
	"CLS\r\n" +
	"SET PRINTER DT\r\n" +
	"TEXT 10,10,\"3\",0,1,1,\"PRICE: 20000\"\r\n" +
	"BARCODE 10,50,\"128\",50,1,0,2,2,\"W12345678\"\r\n" +
	"PRINT 1,1\r\n" +
	"CUT\r\n"
)

ep_out.write(label.encode("ascii"))
print("✅ Label sent")
