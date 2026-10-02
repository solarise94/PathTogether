// Default-reader acceptance probe: NO provider forcing, NO preference changes.
// args: [0]=image path, [1]=output json, [2]=region PNG dir, [3]=project dir,
//       [4],[5]=tissue location as fractions of width/height
import qupath.lib.images.servers.ImageServerProvider
import qupath.lib.regions.RegionRequest
import qupath.lib.projects.Projects
import qupath.lib.projects.ProjectIO
import java.awt.image.BufferedImage
import javax.imageio.ImageIO
import com.google.gson.GsonBuilder

def path = args[0]
def uri = new File(path).toURI()
def regionDir = new File(args[2]); regionDir.mkdirs()
def out = [:]
out.max_memory_bytes = Runtime.getRuntime().maxMemory()
out.qupath_version = qupath.lib.common.GeneralTools.getVersion()
out.bioformats_version = loci.formats.FormatTools.VERSION

// 1) every installed builder's own support verdict (informational)
out.builders = []
for (provider in ImageServerProvider.getInstalledImageServerBuilders()) {
    def row = [provider: provider.getClass().getName()]
    try { row.support = provider.checkImageSupport(uri)?.getSupportLevel() } catch (Throwable e) { row.error = e.toString() }
    out.builders.add(row)
}

// 2) the DEFAULT server QuPath picks (same call the GUI's open path uses)
def server = ImageServerProvider.buildServer(path, BufferedImage.class)
try {
    def md = server.getMetadata()
    out.default_server = server.getClass().getName()
    out.width = server.getWidth(); out.height = server.getHeight()
    out.resolutions = server.nResolutions()
    out.downsamples = server.getPreferredDownsamples().toList()
    out.levels = (0..<server.nResolutions()).collect { [width: md.getLevel(it).getWidth(), height: md.getLevel(it).getHeight(), downsample: md.getLevel(it).getDownsample()] }
    out.is_rgb = server.isRGB()
    out.n_channels = server.nChannels()
    out.pixel_type = server.getPixelType().toString()
    out.channels = server.getMetadata().getChannels().collect { it.getName() }
    out.mpp_x = server.getPixelCalibration().getPixelWidthMicrons()
    out.mpp_y = server.getPixelCalibration().getPixelHeightMicrons()
    out.magnification = md.getMagnification()
    out.region_reads = []
    for (int i = 0; i < server.nResolutions(); i++) {
        def lv = md.getLevel(i)
        double ds = lv.getDownsample()
        int lw = lv.getWidth(), lh = lv.getHeight()
        def boxes = [
            center: [Math.max(0, lw.intdiv(2) - 64), Math.max(0, lh.intdiv(2) - 64), Math.min(128, lw), Math.min(128, lh)],
            corner: [0, 0, Math.min(96, lw), Math.min(96, lh)],
            edge:   [Math.max(0, lw - 80), Math.max(0, lh - 80), Math.min(80, lw), Math.min(80, lh)],
            tissue: [Math.max(0, Math.min(lw - 128, (int)(Double.parseDouble(args[4]) * lw) - 64)), Math.max(0, Math.min(lh - 128, (int)(Double.parseDouble(args[5]) * lh) - 64)), Math.min(128, lw), Math.min(128, lh)],
        ]
        boxes.each { name, b ->
            def req = RegionRequest.createInstance(server.getPath(), ds,
                (int)Math.round(b[0] * ds), (int)Math.round(b[1] * ds),
                (int)Math.round(b[2] * ds), (int)Math.round(b[3] * ds))
            def img = server.readRegion(req)
            def f = new File(regionDir, "l${i}-${name}.png")
            ImageIO.write(img, 'png', f)
            out.region_reads.add([level: i, name: name, level_box: b, width: img.getWidth(), height: img.getHeight(), png: f.getName()])
        }
    }
} finally { server.close() }

// 3) Bio-Formats resolution grouping, explicitly (not via QuPath)
def reader = new loci.formats.ImageReader()
def svc = new loci.common.services.ServiceFactory().getInstance(loci.formats.services.OMEXMLService.class)
def meta = svc.createOMEXMLMetadata()
reader.setMetadataStore(meta)
reader.setFlattenedResolutions(false)
try {
    reader.setId(path)
    def bf = [reader: reader.getReader().getClass().getName(), series: reader.getSeriesCount(), resolutions: reader.getResolutionCount(),
              rgb: reader.isRGB(), rgb_channel_count: reader.getRGBChannelCount(), effective_size_c: reader.getEffectiveSizeC(),
              size_c: reader.getSizeC(), interleaved: reader.isInterleaved(), pixel_type: loci.formats.FormatTools.getPixelTypeString(reader.getPixelType())]
    bf.resolution_sizes = (0..<reader.getResolutionCount()).collect { r -> reader.setResolution(r); [reader.getSizeX(), reader.getSizeY()] }
    reader.setResolution(0)
    bf.physical_size_x = meta.getPixelsPhysicalSizeX(0)?.value()?.doubleValue()
    bf.physical_size_y = meta.getPixelsPhysicalSizeY(0)?.value()?.doubleValue()
    bf.physical_unit = meta.getPixelsPhysicalSizeX(0)?.unit()?.getSymbol()
    bf.objective_nominal_magnification = meta.getInstrumentCount() > 0 && meta.getObjectiveCount(0) > 0 ? meta.getObjectiveNominalMagnification(0, 0) : null
    bf.channel_count = meta.getChannelCount(0)
    bf.channel_samples_per_pixel = (0..<meta.getChannelCount(0)).collect { meta.getChannelSamplesPerPixel(0, it)?.getValue() }
    out.bioformats = bf
} finally { reader.close() }

// 4) project save + reopen with whatever builder the DEFAULT selection produced
def pdir = new File(args[3]); pdir.mkdirs()
def project = Projects.createProject(pdir, BufferedImage.class)
def s2 = ImageServerProvider.buildServer(path, BufferedImage.class)
try {
    def entry = project.addImage(s2.getBuilder())
    entry.setImageName('acceptance sample')
    project.syncChanges()
} finally { s2.close() }
def restored = ProjectIO.loadProject(new File(project.getURI()), BufferedImage.class)
def reopened = restored.getImageList().get(0).getServerBuilder().build()
try {
    def req = RegionRequest.createInstance(reopened.getPath(), reopened.getDownsampleForResolution(reopened.nResolutions() - 1), 0, 0, reopened.getWidth(), reopened.getHeight())
    def img = reopened.readRegion(req)
    out.project_reopen = [server: reopened.getClass().getName(), resolutions: reopened.nResolutions(), width: reopened.getWidth(), height: reopened.getHeight(),
                          mpp_x: reopened.getPixelCalibration().getPixelWidthMicrons(), lowest_level_read: [img.getWidth(), img.getHeight()]]
} finally { reopened.close() }

def json = new GsonBuilder().setPrettyPrinting().serializeSpecialFloatingPointValues().create().toJson(out)
new File(args[1]).text = json
println('PROBE_RESULT ' + json)
