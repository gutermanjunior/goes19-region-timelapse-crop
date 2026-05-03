from PIL import Image, ImageDraw

Image.MAX_IMAGE_PIXELS = None

# Image dimensions: 21696x21696 pixels
# Image center: 10848, 10848

img = Image.open(r"C:\Users\Seu_Usuario\Downloads\20261231850_GOES19-ABI-FD-GEOCOLOR-21696x21696.jpg")


draw = ImageDraw.Draw(img)


region = (14400, 14600, 17500, 16200)  # left, top, right, bottom
draw.rectangle(region, outline="red", width=25)

img.show()